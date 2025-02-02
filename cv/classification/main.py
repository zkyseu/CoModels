"""
Modified from https://github.com/microsoft/Swin-Transformer/blob/main/main.py
"""

import os
import time
import argparse
import datetime
import numpy as np
import oneflow as flow
import oneflow.backends.cudnn as cudnn

from flowvision.loss.cross_entropy import (
    LabelSmoothingCrossEntropy,
    SoftTargetCrossEntropy,
)
from flowvision.utils.metrics import accuracy

from config import get_config
from models import build_model
from data import build_loader
from lr_scheduler import build_scheduler
from optimizer import build_optimizer
from logger import create_logger
from utils import (
    load_checkpoint,
    save_checkpoint,
    get_grad_norm,
    auto_resume_helper,
    reduce_tensor,
    AverageMeter,
    TimeMeter
)


def parse_option():
    parser = argparse.ArgumentParser(
        "Flowvision image classification training and evaluation script", add_help=False
    )
    parser.add_argument(
        "--model_arch",
        type=str,
        required=True,
        default="swin_tiny_patch4_window7_224",
        help="model for training",
    )
    parser.add_argument(
        "--cfg", type=str, required=True, metavar="FILE", help="path to config file",
    )
    parser.add_argument(
        "--opts",
        help="Modify config options by adding 'KEY VALUE' pairs. ",
        default=None,
        nargs="+",
    )

    # easy config modification
    parser.add_argument(
        "--synthetic-data",
        action="store_true",
        dest="synthetic_data",
        help="Use synthetic data",
    )
    parser.add_argument(
        "--epochs", type=int, default=300, help="batch size for single GPU"
    )
    parser.add_argument(
        "--batch-size", type=int, default=128, help="batch size for single GPU"
    )
    parser.add_argument("--data-path", type=str, help="path to dataset")
    parser.add_argument(
        "--zip",
        action="store_true",
        help="use zipped dataset instead of folder dataset",
    )
    parser.add_argument(
        "--cache-mode",
        type=str,
        default="part",
        choices=["no", "full", "part"],
        help="no: no cache, "
        "full: cache all data, "
        "part: sharding the dataset into nonoverlapping pieces and only cache one piece",
    )
    parser.add_argument("--resume", help="resume from checkpoint")
    parser.add_argument(
        "--accumulation-steps", type=int, help="gradient accumulation steps"
    )
    parser.add_argument(
        "--use-checkpoint",
        action="store_true",
        help="whether to use gradient checkpointing to save memory",
    )
    parser.add_argument(
        "--output",
        default="output",
        type=str,
        metavar="PATH",
        help="root of output folder, the full path is <output>/<model_name>/<tag> (default: output)",
    )
    parser.add_argument("--tag", help="tag of experiment")
    parser.add_argument("--eval", action="store_true", help="Perform evaluation only")
    parser.add_argument(
        "--throughput", action="store_true", help="Test throughput only"
    )

    # distributed training
    parser.add_argument(
        "--local_rank",
        type=int,
        default=0,
        required=False,
        help="local rank for DistributedDataParallel",
    )

    args, unparsed = parser.parse_known_args()

    config = get_config(args)

    return args, config


def main(config):
    (
        dataset_train,
        dataset_val,
        data_loader_train,
        data_loader_val,
        mixup_fn,
    ) = build_loader(config)

    logger.info(f"Creating model:{config.MODEL.ARCH}")
    model = build_model(config)
    model.cuda()

    optimizer = build_optimizer(config, model)
    model = flow.nn.parallel.DistributedDataParallel(model, broadcast_buffers=False)
    # FIXME: model with DDP wrapper doesn't have model.module
    model_without_ddp = model

    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info(f"number of params: {n_parameters}")
    if hasattr(model_without_ddp, "flops"):
        flops = model_without_ddp.flops()
        logger.info(f"number of GFLOPs: {flops / 1e9}")

    lr_scheduler = build_scheduler(config, optimizer, len(data_loader_train))

    if config.AUG.MIXUP > 0.0:
        # smoothing is handled with mixup label transform
        criterion = SoftTargetCrossEntropy()
    elif config.MODEL.LABEL_SMOOTHING > 0.0:
        criterion = LabelSmoothingCrossEntropy(smoothing=config.MODEL.LABEL_SMOOTHING)
    else:
        criterion = flow.nn.CrossEntropyLoss()

    max_accuracy = 0.0

    if config.TRAIN.AUTO_RESUME:
        resume_file = auto_resume_helper(config.OUTPUT)
        if resume_file:
            if config.MODEL.RESUME:
                logger.warning(
                    f"auto-resume changing resume file from {config.MODEL.RESUME} to {resume_file}"
                )
            config.defrost()
            config.MODEL.RESUME = resume_file
            config.freeze()
            logger.info(f"auto resuming from {resume_file}")
        else:
            logger.info(f"no checkpoint found in {config.OUTPUT}, ignoring auto resume")

    if config.MODEL.RESUME:
        print("resume called")
        max_accuracy = load_checkpoint(
            config, model_without_ddp, optimizer, lr_scheduler, logger
        )
        acc1, acc5, loss = validate(config, data_loader_val, model)
        logger.info(
            f"Accuracy of the network on the {len(data_loader_val)} test images: {acc1:.1f}%"
        )
        if config.EVAL_MODE:
            return
    
    if config.THROUGHPUT_MODE:
        acc1, acc5, loss = validate(config, data_loader_val, model)
        logger.info(
            f"Accuracy of the network on the {len(data_loader_val)} test images: {acc1:.1f}%"
        )
        throughput(data_loader_val, model, logger)
        return

    logger.info("Start training")
    start_time = time.time()
    for epoch in range(config.TRAIN.START_EPOCH, config.TRAIN.EPOCHS):
        if not config.DATA.SYNTHETIC_DATA:
            data_loader_train.sampler.set_epoch(epoch)

        train_one_epoch(
            config,
            model,
            criterion,
            data_loader_train,
            optimizer,
            epoch,
            mixup_fn,
            lr_scheduler,
        )
        if flow.env.get_rank() == 0 and (
            epoch % config.SAVE_FREQ == 0 or epoch == (config.TRAIN.EPOCHS - 1)
        ):
            save_checkpoint(
                config,
                epoch,
                model_without_ddp,
                max_accuracy,
                optimizer,
                lr_scheduler,
                logger,
            )

        # no validate
        acc1, acc5, loss = validate(config, data_loader_val, model)
        logger.info(
            f"Accuracy of the network on the {len(data_loader_val)} test images: {acc1:.1f}%"
        )
        max_accuracy = max(max_accuracy, acc1)
        logger.info(f"Max accuracy: {max_accuracy:.2f}%")

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    logger.info("Training time {}".format(total_time_str))


def train_one_epoch(
    config, model, criterion, data_loader, optimizer, epoch, mixup_fn, lr_scheduler
):
    model.train()
    optimizer.zero_grad()

    num_steps = len(data_loader)
    one_sample_time = TimeMeter()
    loss_meter = AverageMeter()

    start = time.time()
    end = time.time()
    for idx, (samples, targets) in enumerate(data_loader):
        samples = samples.cuda()
        targets = targets.cuda()

        if mixup_fn is not None:
            samples, targets = mixup_fn(samples, targets)

        outputs = model(samples)

        if config.TRAIN.ACCUMULATION_STEPS > 1:
            loss = criterion(outputs, targets)
            loss = loss / config.TRAIN.ACCUMULATION_STEPS
            loss.backward()
            if config.TRAIN.CLIP_GRAD:
                flow.nn.utils.clip_grad_norm_(
                    model.parameters(), config.TRAIN.CLIP_GRAD
                )
            if (idx + 1) % config.TRAIN.ACCUMULATION_STEPS == 0:
                optimizer.step()
                optimizer.zero_grad()
                lr_scheduler.step()
        else:
            loss = criterion(outputs, targets)
            optimizer.zero_grad()
            loss.backward()
            if config.TRAIN.CLIP_GRAD:
                flow.nn.utils.clip_grad_norm_(
                    model.parameters(), config.TRAIN.CLIP_GRAD
                )
            optimizer.step()
            lr_scheduler.step()

        one_sample_time.record(samples.size(0) * flow.env.get_world_size())
        loss_meter.record(loss.cpu().detach(), targets.size(0))
        
        end = time.time()

        if idx % config.PRINT_FREQ == 0:
            lr = optimizer.param_groups[0]["lr"]
            loss, loss_avg = loss_meter.get()
            throughput, throughput_avg = one_sample_time.get()
            etas =  (num_steps - idx) * samples.size(0) * flow.env.get_world_size() / throughput_avg
            one_sample_time.reset()

            logger.info(
                f"Train: [{epoch}/{config.TRAIN.EPOCHS}][{idx}/{num_steps}]\t"
                f"eta {datetime.timedelta(seconds=int(etas))}\tlr {lr:.6f}\t"
                f"time {samples.size(0) * flow.env.get_world_size() / throughput:.4f}s ({samples.size(0) * flow.env.get_world_size() / throughput_avg:.4f}s)\t"
                f"rate {throughput:.4f}/s ({throughput_avg:.4f}/s)\t"
                f"loss {loss:.4f} ({loss_avg:.4f})\t"
            )

    epoch_time = time.time() - start
    logger.info(
        f"EPOCH {epoch} training takes {datetime.timedelta(seconds=int(epoch_time))}"
    )


@flow.no_grad()
def validate(config, data_loader, model):
    criterion = flow.nn.CrossEntropyLoss()
    model.eval()

    batch_time = TimeMeter()
    loss_meter = AverageMeter()
    acc1_meter = AverageMeter()
    acc5_meter = AverageMeter()

    end = time.time()
    for idx, (images, target) in enumerate(data_loader):
        images = images.cuda()
        target = target.cuda()

        # compute output
        output = model(images)

        # measure accuracy and record loss
        loss = criterion(output, target)
        acc1, acc5 = accuracy(output, target, topk=(1, 5))

        acc1 = reduce_tensor(acc1)
        acc5 = reduce_tensor(acc5)
        loss = reduce_tensor(loss)

        batch_time.record(target.size(0) * flow.env.get_world_size())
        loss_meter.record(loss, target.size(0))
        acc1_meter.record(acc1, target.size(0))
        acc5_meter.record(acc5, target.size(0))

        # measure elapsed time
        end = time.time()

        if idx % config.PRINT_FREQ == 0:
            acc1, acc1_avg = acc1_meter.get()
            acc5, acc5_avg = acc5_meter.get()
            loss, loss_avg = loss_meter.get()
            throughput, throughput_avg = batch_time.get()
            batch_time.reset()

            logger.info(
                f"Test: [{idx}/{len(data_loader)}]\t"
                f"Throughput {throughput:.3f} ({throughput_avg:.3f})\t"
                f"Loss {loss:.4f} ({loss_avg:.4f})\t"
                f"Acc@1 {acc1:.3f} ({acc1_avg:.3f})\t"
                f"Acc@5 {acc5:.3f} ({acc5_avg:.3f})\t"
            )

    logger.info(f" * Acc@1 {acc1_avg:.3f} Acc@5 {acc5_avg:.3f}")
    return acc1_avg, acc5_avg, loss_meter


@flow.no_grad()
def throughput(data_loader, model, logger):
    model.eval()

    for idx, (images, _) in enumerate(data_loader):
        images = images.cuda()
        batch_size = images.shape[0]
        for i in range(50):
            model(images)
        flow.cuda.synchronize()
        # TODO: add flow.cuda.synchronize()
        logger.info(f"throughput averaged with 30 times")
        tic1 = time.time()
        for i in range(30):
            model(images)

        flow.cuda.synchronize()
        tic2 = time.time()
        logger.info(
            f"batch_size {batch_size} throughput {30 * batch_size / (tic2 - tic1)}"
        )
        return


if __name__ == "__main__":
    _, config = parse_option()

    if "RANK" in os.environ and "WORLD_SIZE" in os.environ:
        rank = flow.env.get_rank()
        world_size = flow.env.get_world_size()
        print(f"RANK and WORLD_SIZE in environ: {rank}/{world_size}")
    else:
        rank = -1
        world_size = -1

    seed = config.SEED + flow.env.get_rank()
    flow.manual_seed(seed)
    np.random.seed(seed)
    cudnn.benchmark = True

    linear_scaled_lr = (
        config.TRAIN.BASE_LR
        * config.DATA.BATCH_SIZE
        * flow.env.get_world_size()
        / 512.0
    )
    linear_scaled_warmup_lr = (
        config.TRAIN.WARMUP_LR
        * config.DATA.BATCH_SIZE
        * flow.env.get_world_size()
        / 512.0
    )
    linear_scaled_min_lr = (
        config.TRAIN.MIN_LR * config.DATA.BATCH_SIZE * flow.env.get_world_size() / 512.0
    )

    # gradient accumulation also need to scale the learning rate
    if config.TRAIN.ACCUMULATION_STEPS > 1:
        linear_scaled_lr = linear_scaled_lr * config.TRAIN.ACCUMULATION_STEPS
        linear_scaled_warmup_lr = (
            linear_scaled_warmup_lr * config.TRAIN.ACCUMULATION_STEPS
        )
        linear_scaled_min_lr = linear_scaled_min_lr * config.TRAIN.ACCUMULATION_STEPS
    config.defrost()
    config.TRAIN.BASE_LR = linear_scaled_lr
    config.TRAIN.WARMUP_LR = linear_scaled_warmup_lr
    config.TRAIN.MIN_LR = linear_scaled_min_lr
    config.freeze()

    os.makedirs(config.OUTPUT, exist_ok=True)
    logger = create_logger(
        output_dir=config.OUTPUT,
        dist_rank=flow.env.get_rank(),
        name=f"{config.MODEL.ARCH}",
    )

    if flow.env.get_rank() == 0:
        path = os.path.join(config.OUTPUT, "config.json")
        with open(path, "w") as f:
            f.write(config.dump())
        logger.info(f"Full config saved to {path}")

    # print config
    logger.info(config.dump())

    main(config)
