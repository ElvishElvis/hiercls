import collections
import csv
from functools import partial
import gzip
import json
import pathlib
import pickle
from typing import List, Optional, Sequence, Tuple

from absl import app
from absl import flags
from absl import logging
from jax import tree_util
from ml_collections import config_flags
import ml_collections
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from torch import optim
import torch.utils.data
import torch.utils.tensorboard
import torchvision
from torchvision import transforms

import datasets
import hier
import hier_torch
import metrics
import models.kuangliu_cifar.resnet
import models.moit.preact_resnet
import progmet

EVAL_BATCH_SIZE = 256
SOURCE_DIR = pathlib.Path(__file__).parent
LOADER_PREFETCH_FACTOR = 2
TENSORBOARD_FLUSH_SECS = 10

PREDICT_METHODS = ['leaf', 'majority']

config_flags.DEFINE_config_file('config')

# Standard usage:
# EXP_GROUP=...
# EXP_NAME=...
# --experiment_dir=$EXP_GROUP/$EXP_NAME
# --tensorboard_dir=$EXP_GROUP/tensorboard/$EXP_NAME
flags.DEFINE_string(
    'experiment_dir', None, 'Where to write experiment data.')
flags.DEFINE_string(
    'tensorboard_dir', None,
    'Where to write tensorboard logs. '
    'Logs will be written under a dir for the experiment_id.')

flags.DEFINE_integer(
    'eval_freq', 1, 'Frequency with which to run eval (epochs).')
flags.DEFINE_integer(
    'save_freq', 10, 'Frequency with which to save model and results (epochs).')
flags.DEFINE_integer(
    'loader_num_workers', 16, 'Number of data loaders (affects memory footprint).')
flags.DEFINE_bool(
    'loader_pin_memory', False, 'Use page-locked memory in training data loader.')

flags.DEFINE_bool('resume', False, 'Resume from previous checkpoint.')
flags.DEFINE_bool('skip_initial_eval', True, 'Skip eval for epoch 0.')

FLAGS = flags.FLAGS


def reset_resnet_fc_(num_outputs: int, model: torchvision.models.ResNet) -> torchvision.models.ResNet:
    model.fc = nn.Linear(model.fc.in_features, num_outputs, bias=True)
    return model


MODEL_FNS = {
    'torch_resnet18': lambda num_outputs: (
        torchvision.models.resnet18(pretrained=False, num_classes=num_outputs)),
    'torch_resnet18_pretrain': lambda num_outputs: reset_resnet_fc_(
        num_outputs,
        torchvision.models.resnet18(pretrained=True, num_classes=1000)),
    'torch_resnet50': lambda num_outputs: (
        torchvision.models.resnet50(pretrained=False, num_classes=num_outputs)),
    'torch_resnet50_pretrain': lambda num_outputs: reset_resnet_fc_(
        num_outputs,
        torchvision.models.resnet50(pretrained=True, num_classes=1000)),
    'kuangliu_resnet18': models.kuangliu_cifar.resnet.ResNet18,
    'moit_preact_resnet18': lambda num_outputs: (
        models.moit.preact_resnet.PreActResNet18(num_outputs, mode='')),
}


def make_model(name: str, num_outputs: int) -> nn.Module:
    try:
        model_fn = MODEL_FNS[name]
    except KeyError:
        raise ValueError('unknown model', name)
    return model_fn(num_outputs)


# Functions with signature:
# dataset_fn(root, split, transform=transform)
DATASET_FNS = {
    'imagenet': torchvision.datasets.ImageNet,
    'tiny_imagenet': datasets.TinyImageNet,
    'inaturalist2018': datasets.INaturalist2018,
    'inaturalist2021': datasets.INaturalist2021,
}

# normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
#                                  std=[0.229, 0.224, 0.225])
normalize = transforms.Normalize(0.5, 1.0)

# Non-deterministic transforms have 'rand_' prefix.
TRANSFORMS = {
    # CIFAR train
    'rand_pad4_crop32_hflip': transforms.Compose([
        transforms.Pad(4),
        transforms.RandomCrop(32),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
    ]),
    # CIFAR eval
    'none': transforms.Compose([
        transforms.ToTensor(),
    ]),

    # ImageNet train
    'rand_resizedcrop224_hflip': transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize,
    ]),
    # ImageNet eval
    'resize256_crop224': transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        normalize,
    ]),

    # Tiny ImageNet train (64px)
    'rand_crop56_hflip': transforms.Compose([
        transforms.RandomCrop(56),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
    ]),
    # Tiny ImageNet eval (64px)
    'crop56': transforms.Compose([
        transforms.CenterCrop(56),
        transforms.ToTensor(),
    ]),
}


def make_base_dataset(name, root, split, transform_name):
    try:
        dataset_fn = DATASET_FNS[name]
    except KeyError:
        raise ValueError('unknown dataset', name)

    try:
        transform = TRANSFORMS[transform_name]
    except KeyError:
        raise ValueError('unknown transform', transform_name)

    return dataset_fn(root, split, transform=transform)


def main(_):
    config = FLAGS.config
    experiment_dir = FLAGS.experiment_dir

    if not experiment_dir:
        logging.warning('no experiment_dir; not saving experiment data')
    else:
        # Create experiment directory.
        experiment_dir = pathlib.Path(FLAGS.experiment_dir).absolute()
        if experiment_dir.exists() and not FLAGS.resume:
            raise ValueError('dir exists but resume is not set', str(experiment_dir))
        logging.info('write experiment data to: %s', str(experiment_dir))
        experiment_dir.mkdir(parents=True, exist_ok=True)
        # Dump config to file for future reference.
        config_str = json.dumps(config.to_dict())
        with open(experiment_dir / 'config.json', 'w') as f:
            f.write(config_str)

    train(config, experiment_dir)


class LabelOrder:

    def __init__(self, kind: str, custom_order: Sequence[int] = None):
        assert kind in ('leaf', 'node', 'custom')
        self.kind = kind
        # The custom order is None iff the kind is not 'custom'.
        assert (custom_order is None) == (kind != 'custom')
        self.custom_order = np.array(custom_order)

    def nodes(self, tree: hier.Hierarchy) -> np.ndarray:
        if self.kind == 'leaf':
            return tree.leaf_subset()
        elif self.kind == 'node':
            return np.arange(tree.num_nodes())
        elif self.kind == 'custom':
            assert np.all(self.custom_order < tree.num_nodes())
            return np.array(self.custom_order)
        else:
            raise ValueError('unknown kind')

    def __eq__(a, b):
        assert isinstance(b, LabelOrder)
        return (a.kind == b.kind and np.array_equal(a.custom_order, b.custom_order))


def _reverse_lookup(table: Sequence, values: Sequence, default=None) -> List:
    inv = {x: i for i, x in enumerate(table)}
    return [inv.get(x, default) for x in values]


def _np_reverse_lookup_int(table: np.ndarray, values: np.ndarray, default=None, n=None) -> np.ndarray:
    assert table.ndim == 1
    if np.size(values) == 0:
        return np.empty(values.shape, dtype=int)
    if default is None:
        # Require that every value is present in the table.
        assert np.all(np.isin(values, table))
        default = -1
    if n is None:
        n = np.max(table) + 1
    else:
        assert np.all(table < n)
    assert np.all(table >= 0)
    inv = np.full(n, default, dtype=int)
    inv[table] = np.arange(len(table))
    return inv[values]


LabelMap = collections.namedtuple('LabelMap', ['to_node', 'to_target'])


def make_datasets(config: ml_collections.ConfigDict):
    """Configures datasets, tree, labels.

    The resulting datasets may have different label spaces.
    Returns one label->node map each for the train and eval datasets
    as well as a label->target map for the train dataset.
    If the label->node maps are equal, loss can be evaluated during eval.

    If `train_with_leaf_targets` is True then the loss function will receive leaf indices.
    In this case, it is assumed that the dataset contains only leaf-node labels.
    If `train_with_leaf_targets` is False, then the loss function will receive node indices.
    The targets in the dataset match what the loss function will receive.

    If `label_order` is specified (and `train_with_leaf_targets` is False), then a mapping
    from labels to nodes will be taken from a corresponding file.
    This is required for e.g. ImageNet21k.

    If `train_subset` is True, then a subtree is derived that contains those nodes.
    If `keep_examples` is False, then the examples are excluded.
    If `keep_examples` is True, then the examples are kept with projected labels.
    This requires that `train_with_leaf_targets` is False.

    If `train_labels` is specified, the labels in the dataset will be replaced
    with labels from a corresponding file.
    This is used for label degradation.
    TODO: Maybe do this at runtime instead of from file?
    """

    tree, node_names = load_hierarchy(config.hierarchy)

    # Start with the original mapping from labels to nodes. In most cases,
    # this is the leaf nodes in the order that they appear in the edge list.
    # However, some datasets may specify labels in a different order, e.g. ImageNet21k.
    # TODO: Add some way to set this.
    # TODO: Permit to train with original labels even if not leaf nodes?
    label_to_node = tree.leaf_subset()

    train_dataset = make_base_dataset(
        config.dataset,
        root=config.dataset_root,
        split=config.train_split,
        transform_name=config.train_transform)
    eval_dataset = make_base_dataset(
        config.dataset,
        root=config.dataset_root,
        split=config.eval_split,
        transform_name=config.eval_transform)

    if config.train_labels and config.train_subset:
        raise ValueError('cannot both override labels and take subset of classes')

    if config.train_subset:
        # Take subtree using the subset of nodes.
        assert hasattr(train_dataset, 'targets'), 'need targets to take class subset'
        min_node_subset = load_node_subset(config.train_subset, node_names)
        subtree, node_subset, project_to_subtree = hier.subtree(tree, min_node_subset)
        if not config.keep_examples:
            # Take a subset of the train_dataset.
            # For each example, check whether the node was kept in the subtree.
            example_subset, = np.nonzero(np.isin(label_to_node[train_dataset.targets], node_subset))
            train_dataset = torch.utils.data.Subset(train_dataset, example_subset)
        # Remap the labels in the training set for the subtree.
        if config.train_with_leaf_targets:
            # We still want to use leaf targets for training.
            # This is only possible if we exclude the examples from other classes.
            assert not config.keep_examples, 'cannot keep examples and use leaf targets'
            # During eval, we always keep all examples.
            # This means that we cannot evaluate the loss during eval.
            train_label_map = LabelMap(
                to_node=project_to_subtree[label_to_node],
                to_target=_np_reverse_lookup_int(
                    subtree.leaf_subset(), project_to_subtree[label_to_node], default=-1))
            eval_label_map = LabelMap(
                to_node=project_to_subtree[label_to_node],
                to_target=None)
        else:
            train_label_map = eval_label_map = LabelMap(
                to_node=project_to_subtree[label_to_node],
                to_target=project_to_subtree[label_to_node])
        # Replace tree with subtree.
        tree = subtree

    elif config.train_labels:
        # Replace the training dataset by a dataset with modified labels.
        # The modified labels can be any node, therefore not possible to use leaf targets.
        assert not config.train_with_leaf_targets, 'cannot override labels and use leaf targets'
        # Override the
        targets = load_labels(config.train_labels, node_names)
        # Check consistency of new labels.
        assert len(targets) == len(train_dataset)
        is_ancestor = tree.ancestor_mask()
        assert np.all(is_ancestor[targets, label_to_node[train_dataset.targets]])
        logging.info('new labels are ancestors of original labels')
        train_dataset = datasets.OverrideTargets(train_dataset, targets)
        # The label space is the set of nodes.
        # After overriding the targets in the training set, the labels are already nodes.
        # However, the eval labels are unchanged and we need to map them to nodes.
        train_label_map = LabelMap(
            to_node=np.arange(tree.num_nodes()),
            to_target=np.arange(tree.num_nodes()))
        eval_label_map = LabelMap(
            to_node=label_to_node,
            to_target=label_to_node)

    else:
        if not config.train_with_leaf_targets:
            # Use nodes as targets.
            train_label_map = eval_label_map = LabelMap(
                to_node=label_to_node,
                to_target=label_to_node)
        else:
            # Use identity mapping to obtain targets.
            # TODO: Would prefer not to do trivial lookup but None means no map exists.
            # May avoid branching anyway?
            train_label_map = eval_label_map = LabelMap(
                to_node=label_to_node,
                to_target=np.arange(tree.num_leaf_nodes()))

    # Convert target map to torch tensor.
    train_label_map = LabelMap(
        to_node=train_label_map.to_node,
        to_target=torch.from_numpy(train_label_map.to_target))
    if eval_label_map.to_target is not None:
        eval_label_map = LabelMap(
            to_node=eval_label_map.to_node,
            to_target=torch.from_numpy(eval_label_map.to_target))

    return train_dataset, eval_dataset, tree, train_label_map, eval_label_map


def load_hierarchy(hierarchy_tag):
    fname = SOURCE_DIR / f'resources/hierarchy/{hierarchy_tag}.csv'
    with open(fname) as f:
        tree, node_names = hier.make_hierarchy_from_edges(hier.load_edges(f))
    return tree, node_names


def load_node_subset(subset_tag, node_names):
    fname = SOURCE_DIR / f'resources/class_subset/{subset_tag}.txt'
    with open(fname) as f:
        name_subset = [line.strip() for line in f]
    # Take sub-tree of original tree.
    name_to_node = {name: i for i, name in enumerate(node_names)}
    node_subset = [name_to_node[name] for name in name_subset]
    return node_subset


def load_labels(labels_tag, node_names):
    name_to_node = {name: i for i, name in enumerate(node_names)}
    fname = SOURCE_DIR / f'resources/override_labels/{labels_tag}.csv.gz'
    with gzip.open(fname, 'rt') as f:
        rows = list(csv.reader(f))
    return np.array([name_to_node[new_name] for new_name, _ in rows])


def train(config, experiment_dir: Optional[pathlib.Path]):
    device = torch.device('cuda')

    train_dataset, eval_dataset, tree, train_label_map, eval_label_map = make_datasets(config)

    train_loader = torch.utils.data.DataLoader(
        dataset=train_dataset,
        batch_size=config.train.batch_size,
        shuffle=True,
        pin_memory=FLAGS.loader_pin_memory,
        num_workers=FLAGS.loader_num_workers,
        prefetch_factor=LOADER_PREFETCH_FACTOR)
    eval_loader = torch.utils.data.DataLoader(
        dataset=eval_dataset,
        batch_size=EVAL_BATCH_SIZE,
        shuffle=False,
        pin_memory=False,  # FLAGS.loader_pin_memory,
        num_workers=FLAGS.loader_num_workers,
        prefetch_factor=LOADER_PREFETCH_FACTOR)

    # TODO: Create a factory for this?
    if config.predict == 'flat_softmax':
        num_outputs = tree.num_leaf_nodes()
        if config.train_with_leaf_targets:
            loss_fn = partial(F.cross_entropy, label_smoothing=config.train.label_smoothing)
        else:
            if config.train.label_smoothing:
                raise NotImplementedError
            loss_fn = hier_torch.FlatLogSoftmaxNLL(tree).to(device)
        pred_fn = partial(
            lambda sum_fn, theta: sum_fn(F.softmax(theta, dim=-1), dim=-1),
            hier_torch.SumLeafDescendants(tree, strict=False).to(device))

    elif config.predict == 'hier_softmax':
        num_outputs = tree.num_nodes() - 1
        # loss_fn = partial(hier_torch.hier_softmax_nll_with_leaf, tree)
        # loss_fn = hier_torch.HierSoftmaxNLL(tree, with_leaf_targets=True).to(device)
        if config.train.hier_normalize == '':
            assert not config.train.hier_normalize
            node_weight = None
        # elif config.train.hier_normalize == 'self':
        #     node_weight = torch.from_numpy(
        #         (1 / hier.uniform_leaf(tree)) * (1 / tree.num_nodes()))
        elif config.train.hier_normalize == 'parent':
            parent_mass = hier.uniform_leaf(tree)[tree.parents(root_loop=True)]
            node_weight = torch.from_numpy((1 / parent_mass) * (1 / tree.num_conditionals()))
        elif config.train.hier_normalize == 'sqrt_parent':
            parent_mass = hier.uniform_leaf(tree)[tree.parents(root_loop=True)]
            node_weight = torch.from_numpy(np.sqrt((1 / parent_mass) * (1 / tree.num_conditionals())))
        else:
            raise ValueError('unknown hier_normalize', config.train.hier_normalize)
        loss_fn = hier_torch.HierSoftmaxCrossEntropy(
            tree, with_leaf_targets=config.train_with_leaf_targets,
            label_smoothing=config.train.label_smoothing,
            node_weight=node_weight).to(device)
        pred_fn = partial(
            lambda log_softmax_fn, theta: log_softmax_fn(theta).exp(),
            # partial(hier_torch.hier_log_softmax, tree)
            hier_torch.HierLogSoftmax(tree).to(device))

    elif config.predict == 'bertinetto_hxe':
        if config.train.label_smoothing:
            raise NotImplementedError
        num_outputs = tree.num_nodes() - 1
        loss_fn = hier_torch.BertinettoHXE(
            tree, config.train.hxe_alpha,
            with_leaf_targets=config.train_with_leaf_targets).to(device)
        pred_fn = partial(
            lambda log_softmax_fn, theta: torch.exp(log_softmax_fn(theta)),
            hier_torch.HierLogSoftmax(tree).to(device))

    elif config.predict == 'multilabel':
        if config.train.label_smoothing:
            raise NotImplementedError
        num_outputs = tree.num_nodes() - 1
        loss_fn = hier_torch.MultiLabelNLL(
            tree, with_leaf_targets=config.train_with_leaf_targets).to(device)
        pred_fn = partial(hier_torch.multilabel_likelihood, tree)

    # elif config.predict == 'hier_sigmoid':
    #     if config.train.label_smoothing:
    #         raise NotImplementedError
    #     num_outputs = tree.num_nodes() - 1
    #     loss_fn = hier_torch.HierSigmoidNLL(tree, with_leaf_targets=True).to(device)
    #     pred_fn = partial(
    #         lambda log_sigmoid_fn, theta: torch.exp(log_sigmoid_fn(theta)),
    #         hier_torch.HierLogSigmoid(tree).to(device))

    else:
        raise ValueError('unknown predict method', config.predict)

    net = make_model(config.model, num_outputs)
    net.to(device)

    optimizer = optim.SGD(
        net.parameters(),
        lr=config.train.learning_rate,
        momentum=config.train.momentum,
        weight_decay=config.train.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=config.train.num_epochs)
    if config.train.warmup_epochs:
        # Multiplier will be 1/(n+1), 2/(n+1), ..., n/(n+1), 1, 1, 1.
        warmup_scheduler = torch.optim.lr_scheduler.LinearLR(
            optimizer,
            start_factor=1. / (config.train.warmup_epochs + 1),
            total_iters=config.train.warmup_epochs)
        scheduler = torch.optim.lr_scheduler.ChainedScheduler([scheduler, warmup_scheduler])

    # Metric functions map gt, pr -> scalar (arrays).
    # TODO: Could avoid recomputation of LCA for each metric.
    info_metric = metrics.UniformLeafInfoMetric(tree)
    depth_metric = metrics.DepthMetric(tree)
    metric_fns = {
        'exact': lambda gt, pr: pr == gt,
        'correct': metrics.IsCorrect(tree),
        'info_excess': info_metric.excess,
        'info_deficient': info_metric.deficient,
        'info_dist': info_metric.dist,
        'info_lca': info_metric.value_at_lca,
        'info_gt': info_metric.value_at_gt,
        'info_pr': info_metric.value_at_pr,
        'depth_excess': depth_metric.excess,
        'depth_deficient': depth_metric.deficient,
        'depth_dist': depth_metric.dist,
        'depth_lca': depth_metric.value_at_lca,
        'depth_gt': depth_metric.value_at_gt,
        'depth_pr': depth_metric.value_at_pr,
    }

    # Use tensorboard_dir if specified, otherwise use default (logs/...).
    writer = torch.utils.tensorboard.SummaryWriter(
        FLAGS.tensorboard_dir or None,
        flush_secs=TENSORBOARD_FLUSH_SECS)

    is_leaf = tree.leaf_mask()
    specificity = -tree.num_leaf_descendants()
    node_mask = (tree.num_children() != 1)

    # Loop for one extra epoch to save and evaluate model.
    for epoch in range(config.train.num_epochs + 1):
        epoch_str = f'{epoch:04d}'

        if experiment_dir is not None and epoch % FLAGS.save_freq == 0:
            # Save model and results to filesystem.
            checkpoint_file = experiment_dir / f'checkpoints/epoch-{epoch_str}.pth'
            logging.info('write checkpoint: %s', checkpoint_file)
            checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
            torch.save(net.state_dict(), checkpoint_file)

        if (epoch > 0 and epoch % FLAGS.eval_freq == 0) or (epoch == 0 and not FLAGS.skip_initial_eval):
            find_lca = hier.FindLCA(tree)

            # Counts and totals to obtain means.
            example_count = 0
            total_loss = 0.
            metric_totals = {method: {field: 0 for field in metric_fns} for method in PREDICT_METHODS}
            # Per-example predictions to write to filesystem.
            outputs = {
                'gt': [],  # Node in hierarchy.
                'original_label': [],  # Index in label set (for debug).
                # 'label': [],  # Index in label set (for debug).
                'pred': {method: [] for method in PREDICT_METHODS},
                'max_leaf_prob': [],
                'metric': {method: {field: [] for field in metric_fns} for method in PREDICT_METHODS},
            }
            # Sequence-per-example predictions. Cannot be concatenated due to ragged shape.
            seq_outputs = {
                'pred': [],
                'prob': [],
                'metric': {field: [] for field in metric_fns},
            }
            # Voluminous per-example predictions. To be written to a separate file.
            full_outputs = {'prob': []}

            net.eval()
            with torch.inference_mode():
                meter = progmet.ProgressMeter(f'eval epoch {epoch}', interval_time=5, num_div=5)
                for minibatch in meter(eval_loader):
                    inputs, gt_labels = minibatch
                    batch_len = len(inputs)
                    inputs = inputs.to(device)
                    theta = net(inputs)
                    # Can only evaluate loss if there exists a mapping to targets.
                    if eval_label_map.to_target is not None:
                        gt_targets = eval_label_map.to_target[gt_labels]
                        assert torch.all(gt_targets >= 0)
                        loss = loss_fn(theta, gt_targets.to(device))
                        total_loss += batch_len * loss.item()  # TODO: Avoid assuming mean?
                    prob = pred_fn(theta).cpu().numpy()
                    pred = {}
                    pred['leaf'] = argmax_where(prob, is_leaf)
                    max_leaf_prob = max_where(prob, is_leaf)
                    pred['majority'] = arglexmin_where(np.broadcast_arrays(-prob, -specificity),
                                                       (prob > 0.5) & node_mask)
                    # Truncate predictions where more specific than ground-truth.
                    # (Only necessary if some labels are not leaf nodes.)
                    # TODO: Need to do for metric sequences as well!
                    gt_node = eval_label_map.to_node[gt_labels]
                    pred = {k: hier.truncate_given_lca(gt_node, pr, find_lca(gt_node, pr))
                            for k, pr in pred.items()}

                    example_count += batch_len
                    outputs['gt'].append(gt_node)
                    # outputs['label'].append(labels)
                    outputs['original_label'].append(gt_labels)
                    outputs['max_leaf_prob'].append(max_leaf_prob)
                    full_outputs['prob'].append(prob)
                    for method in PREDICT_METHODS:
                        outputs['pred'][method].append(pred[method])
                        for field in metric_fns:
                            metric_fn = metric_fns[field]
                            metric_value = metric_fn(gt_node, pred[method])
                            outputs['metric'][method][field].append(metric_value)
                            metric_totals[method][field] += metric_value.sum()

                    pred_seqs = [prediction_sequence(specificity, p, threshold=0.5, condition=node_mask)
                                 for p in prob]
                    # TODO: Could vectorize if necessary.
                    prob_seqs = [prob[i, pred_i] for i, pred_i in enumerate(pred_seqs)]
                    seq_outputs['pred'].extend(pred_seqs)
                    seq_outputs['prob'].extend(prob_seqs)
                    for field in metric_fns:
                        metric_fn = metric_fns[field]
                        # TODO: Could vectorize if necessary.
                        metric_seqs = [metric_fn(gt_node[i], pred_seqs[i]) for i in range(batch_len)]
                        seq_outputs['metric'][field].extend(metric_seqs)

            if eval_label_map.to_target is not None:
                # Can only evaluate loss if all labels are leaf nodes.
                mean_loss = total_loss / example_count
                writer.add_scalar('loss/eval', mean_loss, epoch)
                logging.info('eval loss: %.5g', mean_loss)
            metric_means = {method: {field: metric_totals[method][field] / example_count
                                     for field in metric_totals[method]} for method in pred}
            for method in pred:
                for field in metric_fns:
                    writer.add_scalar(f'{field}/{method}/eval', metric_means[method][field], epoch)
            logging.info(
                'leaf: exact %.3f, correct %.3f, depth_dist %.4g, info_dist %.4g',
                metric_means['leaf']['exact'], metric_means['leaf']['correct'],
                metric_means['leaf']['depth_dist'], metric_means['leaf']['info_dist'])
            logging.info(
                'majority: exact %.3f, correct %.3f, depth_dist %.4g, info_dist %.4g',
                metric_means['majority']['exact'], metric_means['majority']['correct'],
                metric_means['majority']['depth_dist'], metric_means['majority']['info_dist'])

            # Concatenate minibatches into large array.
            leaf_predicate = lambda x: not isinstance(x, dict)  # Lists are values not containers.
            outputs = tree_util.tree_map(np.concatenate, outputs, is_leaf=leaf_predicate)
            full_outputs = tree_util.tree_map(np.concatenate, full_outputs, is_leaf=leaf_predicate)

            # TODO: Avoid memory consumption when not writing outputs to filesystem.
            if experiment_dir is not None and epoch % FLAGS.save_freq == 0:
                # Write data to filesystem.
                # Scalar outputs per example.
                path = experiment_dir / f'predictions/output-epoch-{epoch_str}.pkl'
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, 'wb') as f:
                    pickle.dump(outputs, f)
                # Sequence of operating points per example.
                path = experiment_dir / f'predictions/operating-points-epoch-{epoch_str}.pkl'
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, 'wb') as f:
                    pickle.dump(seq_outputs, f)
                # Write raw predictions to filesystem.
                path = experiment_dir / f'predictions/full-output-epoch-{epoch_str}.pkl'
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, 'wb') as f:
                    pickle.dump(full_outputs, f)

        if not epoch < config.train.num_epochs:
            break

        # Train one epoch.
        total_loss = 0.
        metric_totals = {method: {field: 0 for field in metric_fns}
                         for method in PREDICT_METHODS}
        step_count = 0
        example_count = 0
        net.train()
        meter = progmet.ProgressMeter(f'train epoch {epoch}', interval_time=5, num_div=5)
        for minibatch in meter(train_loader):
            inputs, gt_labels = minibatch
            batch_len = len(inputs)
            gt_targets = train_label_map.to_target[gt_labels]
            assert torch.all(gt_targets >= 0)
            # TODO: Assert that all labels are non-negative?
            optimizer.zero_grad()
            theta = net(inputs.to(device))
            loss = loss_fn(theta, gt_targets.to(device))
            loss.backward()
            optimizer.step()
            loss = loss.item()  # This will block.
            logging.debug('loss: %g', loss)
            if np.isnan(loss):
                raise ValueError('loss is nan')
            if np.isinf(loss):
                raise ValueError('loss is inf')
            total_loss += loss
            step_count += 1

            # Evaluate metrics for batch predictions and add to totals.
            with torch.no_grad():
                prob = pred_fn(theta)
            prob = prob.cpu().numpy()
            pred = {}
            pred['leaf'] = argmax_where(prob, is_leaf)
            pred['majority'] = arglexmin_where(np.broadcast_arrays(-prob, -specificity),
                                               (prob > 0.5) & node_mask)
            gt_node = train_label_map.to_node[gt_labels]

            for method in PREDICT_METHODS:
                for field in metric_fns:
                    metric_fn = metric_fns[field]
                    metric_totals[method][field] += metric_fn(gt_node, pred[method]).sum()
            example_count += batch_len

        mean_loss = total_loss / step_count
        writer.add_scalar('loss/train', mean_loss, epoch)
        logging.info('train loss: %.5g', mean_loss)
        metric_means = {method: {field: metric_totals[method][field] / example_count
                                 for field in metric_totals[method]} for method in pred}
        for method in pred:
            for field in metric_fns:
                writer.add_scalar(f'{field}/{method}/train', metric_means[method][field], epoch)
        logging.info(
            'leaf: exact %.3f, correct %.3f, depth_dist %.4g, info_dist %.4g',
            metric_means['leaf']['exact'], metric_means['leaf']['correct'],
            metric_means['leaf']['depth_dist'], metric_means['leaf']['info_dist'])
        logging.info(
            'majority: exact %.3f, correct %.3f, depth_dist %.4g, info_dist %.4g',
            metric_means['majority']['exact'], metric_means['majority']['correct'],
            metric_means['majority']['depth_dist'], metric_means['majority']['info_dist'])

        scheduler.step()


def argmax_where(
        value: np.ndarray,
        condition: np.ndarray,
        axis: int = -1,
        keepdims: bool = False) -> np.ndarray:
    # Will raise an exception if not np.all(np.any(condition, axis=axis)).
    return np.nanargmax(np.where(condition, value, np.nan), axis=axis, keepdims=keepdims)


def max_where(
        value: np.ndarray,
        condition: np.ndarray,
        axis: int = -1,
        keepdims: bool = False) -> np.ndarray:
    assert np.all(np.any(condition, axis=axis)), 'require at least one valid element'
    return np.nanmax(np.where(condition, value, np.nan), axis=axis, keepdims=keepdims)


def arglexmin(keys: Tuple[np.ndarray, ...], axis: int = -1) -> np.ndarray:
    order = np.lexsort(keys, axis=axis)
    return np.take(order, 0, axis=axis)


def arglexmin_where(
        keys: Tuple[np.ndarray, ...],
        condition: np.ndarray,
        axis: int = -1,
        keepdims: bool = False,
        ) -> np.ndarray:
    # TODO: Make more efficient (linear rather than log-linear).
    assert np.all(np.any(condition, axis=axis)), 'require at least one valid element'
    order = np.lexsort(keys, axis=axis)
    # Take first element in order that satisfies condition.
    # TODO: Would be faster to take subset and then sort?
    # Would this break the vectorization?
    first_valid = np.argmax(np.take_along_axis(condition, order, axis=axis),
                            axis=axis, keepdims=True)
    result = np.take_along_axis(order, first_valid, axis=axis)
    if not keepdims:
        result = np.squeeze(result, axis=axis)
    return result


def sorted_above_threshold(p: np.ndarray, threshold: float = 0.5) -> np.ndarray:
    """Returns decreasing order of p such that threshold is satisfied.

    Equivalent to prediction_sequence when p is hierarchical and threshold >= 0.5.
    """
    assert p.ndim == 1
    subset, = np.nonzero(p > threshold)
    order = np.argsort(-p[subset])
    return subset[order]


def prediction_sequence(
        specificity: np.ndarray,
        p: np.ndarray,
        threshold: Optional[float] = None,
        condition: Optional[np.ndarray] = None,
        ) -> np.ndarray:
    assert p.ndim == 1
    assert specificity.ndim == 1

    is_valid = np.ones(p.shape, dtype=bool)
    if threshold is not None:
        is_valid &= (p > threshold)
    if condition is not None:
        is_valid &= condition
    assert np.any(is_valid), 'require at least one valid element'

    # Order by confidence (desc) then by specificity (desc).
    # Note that np.lexsort() orders first by the last key.
    # (Performs stable sort from first key to last key.)
    order = np.lexsort((-specificity[is_valid], -p[is_valid]))
    valid_inds, = np.nonzero(is_valid)
    order = valid_inds[order]

    # Guaranteed that: i < j implies p[i] >= p[j]
    # Want also: i < j implies specificity[i] <= specificity[j]
    # That is, we have decreasing scores but increasing specificity.
    ordered_specificity = specificity[order]
    max_specificity = np.maximum.accumulate(ordered_specificity)
    # keep = ordered_specificity >= max_specificity
    # Keep only the first element which satisfies the non-strict inequality.
    keep = np.concatenate([[True], ordered_specificity[1:] > max_specificity[:-1]])
    return order[keep]


def pool_operating_points(example_scores, example_metrics):
    # Obtain order of scores.
    step_scores = np.concatenate([seq[1:] for seq in example_scores])
    step_order = np.argsort(-step_scores)
    step_scores = step_scores[step_order]
    metric_seqs = {}
    for field in example_metrics:
        # Get metric sequence for each example.
        example_seqs = example_metrics[field]
        example_seqs = [seq.astype(float) for seq in example_seqs]
        total_init = np.sum([seq[0] for seq in example_seqs])
        total_deltas = np.concatenate([np.diff(seq) for seq in example_seqs])[step_order]
        metric_seqs[field] = total_init + cumsum_with_zero(total_deltas)
    return metric_seqs, step_scores


def cumsum_with_zero(x: np.ndarray, axis: int = 0) -> np.ndarray:
    ndim = x.ndim
    pad_width = [(0, 0)] * ndim
    pad_width[axis] = (1, 0)
    return np.cumsum(np.pad(x, pad_width, 'constant'), axis=axis)


if __name__ == '__main__':
    app.run(main)
