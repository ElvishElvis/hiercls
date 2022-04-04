EXP_ROOT=../experiments/2022-03-05-imagenet-acc/

EP=15 ; LR=0.01 ; WD=0.0003 ; BATCH=64 ; EXP_NAME=flat-lr-${LR}-wd-${WD}-ep-${EP}-b-${BATCH} ; ../env/bin/python main.py --experiment_dir=${EXP_ROOT}/${EXP_NAME} --tensorboard_dir=${EXP_ROOT}/tensorboard/${EXP_NAME} --config=configs/imagenet.py --config.dataset_root=/home/jack/data/torchvision/imagenet/ --config.model=torch_resnet50 --config.predict=flat_softmax --config.train.batch_size=${BATCH} --config.train.learning_rate=${LR} --config.train.weight_decay=${WD} --config.train.num_epochs=${EP} --config.train_with_leaf_targets=false --loader_num_workers=8 --save_freq=5

EP=15 ; LR=0.01 ; WD=0.0003 ; BATCH=64 ; EXP_NAME=hier-lr-${LR}-wd-${WD}-ep-${EP}-b-${BATCH} ; ../env/bin/python main.py --experiment_dir=${EXP_ROOT}/${EXP_NAME} --tensorboard_dir=${EXP_ROOT}/tensorboard/${EXP_NAME} --config=configs/imagenet.py --config.dataset_root=/home/jack/data/torchvision/imagenet/ --config.model=torch_resnet50 --config.predict=hier_softmax --config.train.batch_size=${BATCH} --config.train.learning_rate=${LR} --config.train.weight_decay=${WD} --config.train.num_epochs=${EP} --config.train_with_leaf_targets=false --loader_num_workers=8 --save_freq=5