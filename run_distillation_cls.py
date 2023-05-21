# -*- coding: utf-8 -*-

import os
import re
import csv
import copy
import math
import argparse

import torch
import torch.distributed as dist
import torch.cuda.amp as amp
from torch.utils.data import DataLoader
from torch.nn.parallel import DistributedDataParallel

import transformers
from transformers import AdamW, get_scheduler

from filelock import FileLock
from tqdm.auto import tqdm

from data import get_reader_class, get_pipeline_class, Dataset, DistributedDataset
from metrics import get_metric_fn
from models import get_model_class
from utils import set_seed, add_kwargs_to_config, Logger, AverageMeter


logger = Logger()


def gather(tensor):
    output_tensors = [torch.zeros_like(tensor) for _ in range(dist.get_world_size())]
    dist.all_gather(output_tensors, tensor)
    concat = torch.cat(output_tensors, dim=0)
    # output = concat[:num_examples] # Truncate dummy elements added by DistributedSampler.
    return output


name_map = {
    "cola": "CoLA",
    "sst2": "SST-2",
    "mrpc": "MRPC",
    "stsb": "STS-B",
    "qqp": "QQP",
    "mnli": "MNLI-m",
    "mnlimm": "MNLI-mm",
    "qnli": "QNLI",
    "rte": "RTE",
    "wnli": "WNLI",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Distil a transformers model on a classification task.")
    parser.add_argument(
        "--model_type",
        type=str,
        required=True,
        help="Type of pretrained model, for indexing model class.",   
    )
    parser.add_argument( # We'd better download the model for ease of use.
        "--teacher_model_path",
        type=str,
        required=True,
        help="Path to pretrained teacher model.",    
    )
    parser.add_argument(
        "--task_name",
        type=str,
        required=True,
        help="The task to train on, for indexing data reader.",
    )
    parser.add_argument(
        "--data_type",
        type=str,
        required=True,
        help="Type of formatted data, for indexing data pipeline.",
    )
    parser.add_argument( # {cls}{text_a}这里的{text_b}看起来{mask}好。{sep}
        "--template",
        type=str,
        default="",
        help="Template for constructing the prompt.",
    )
    parser.add_argument( # {"-1": "不", "0": "较", "1": "很"}
        "--verbalizer",
        type=str,
        default="",
        help="Verbalizer for constructing the prompt.",
    )
    parser.add_argument(
        "--data_dir",
        type=str,
        default="datasets",
        help="Where to load a dataset.",
    )
    parser.add_argument(
        "--output_dir", 
        type=str, 
        default="outputs", 
        help="Where to store the final model.",
    )
    parser.add_argument(
        "--max_length",
        type=int,
        default=128,
        help=(
            "The maximum total input sequence length after tokenization. Sequences longer than this will be truncated,"
            " sequences shorter will be padded."
        ),
    )
    parser.add_argument(
        "--use_slow_tokenizer",
        action="store_true",
        help="If passed, will use a slow tokenizer (not backed by the 🤗 Tokenizers library).",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=32,
        help="Batch size (per device) for the training loader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=32,
        help="Batch size (per device) for the evaluation loader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay", type=float, default=1e-2, help="Weight decay to use.")
    parser.add_argument("--log_interval", type=int, default=1000, help="Interval of logging and possible saving.")
    parser.add_argument("--num_train_epochs", type=int, default=25, help="Total number of training epochs to perform.")
    parser.add_argument("--num_patience_epochs", type=int, default=3, help="Total number of patience epochs for early stop.")
    parser.add_argument(
        "--num_grad_accum_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=str,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--warmup_proportion", type=float, default=0.1, help="Proportion of the warmup in the lr scheduler."
    )
    parser.add_argument(
        "--max_grad_norm", type=float, default=1.0, help="Maximum norm of gradients."
    )
    parser.add_argument(
        "--selection_metric", type=str, default="f1", help="Metric for selection criterion."
    )
    parser.add_argument("--seed", type=int, default=776, help="A seed for reproducible training.")
    parser.add_argument("--local_rank", type=int, default=-1, help="Local rank for distributed training.")
    parser.add_argument("--use_fp16", action="store_true", help="Use FP16 or not.")
    parser.add_argument("--use_cpu", action="store_true", help="Use CPU or not.")
    parser.add_argument("--do_train", action="store_true", help="Do train or not.")
    parser.add_argument("--do_test", action="store_true", help="Do test or not.")
    parser.add_argument("--do_infer", action="store_true", help="Do infer or not.")
    parser.add_argument("--model_suffix", type=str, default="none", help="Suffix for outputs.")

    parser.add_argument("--num_relation_heads", type=int, default=32, help="Num of relation heads so that relation scores can be aligned for minilm.")
    parser.add_argument("--layer_or_sparsity", type=str, default="4L", help="Layer or sparsity for the student.")

    args = parser.parse_args()
    return args


def main():
    args = parse_args()

    args.output_dir = os.path.join(args.output_dir, f"{args.model_type}_{args.model_suffix}_{args.layer_or_sparsity}_{args.task_name}")
    os.makedirs(args.output_dir, exist_ok=True)
    args.data_dir = os.path.join(args.data_dir, args.task_name)

    is_dist = (args.local_rank != -1)
    is_main = (args.local_rank == -1 or args.local_rank == 0)
    is_fp16 = is_dist and args.use_fp16
    device = torch.device("cpu") if args.use_cpu else torch.device("cuda")

    if is_dist:
        # Initialize DDP.
        dist.init_process_group(backend='nccl')
        # Pin GPU to be used to process local rank (one GPU per process).
        torch.cuda.set_device(args.local_rank)

    # Setup logging, we only want one process per machine to log things on the screen.
    logger.add_stream_handler()
    logger.add_file_handler(args.output_dir)
    if is_main:
        logger.set_verbosity_info() 
    else:
        logger.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

    # Load metric functin and data reader.
    metric_fn = get_metric_fn(args.task_name)
    data_reader = get_reader_class(args.task_name)(args.data_dir)
    label_map, reverse_label_map, num_labels = data_reader.get_label_map()

    # Get classes which shall be used.
    tokenizer_class, config_class, model_class = get_model_class(args.model_type)
    pipeline_class = get_pipeline_class(args.data_type)
    
    # Train is conducted in certain acceleration.
    if args.do_train:
        # Load pretrained tokenizer with necessary resizing.
        tokenizer = tokenizer_class.from_pretrained(args.teacher_model_path, use_fast=not args.use_slow_tokenizer)
        
        # Data pipeline.
        data_pipeline = pipeline_class(tokenizer, label_map, args.max_length)

        train_examples = data_reader.get_train_examples()
        train_examples = data_pipeline.build(train_examples, template=args.template, verbalizer=args.verbalizer)

        dev_examples = data_reader.get_dev_examples()
        dev_examples = data_pipeline.build(dev_examples, template=args.template, verbalizer=args.verbalizer)

        if is_dist:
            train_dataset = DistributedDataset(train_examples, shuffle=True)
        else:
            train_dataset = Dataset(train_examples, shuffle=True)
        train_loader = DataLoader(train_dataset, batch_size=args.per_device_train_batch_size, collate_fn=data_pipeline.collate)
        
        if is_dist:
            dev_dataset = DistributedDataset(dev_examples, shuffle=False)
        else:
            dev_dataset = Dataset(dev_examples, shuffle=False)
        dev_loader = DataLoader(dev_dataset, batch_size=args.per_device_eval_batch_size, collate_fn=data_pipeline.collate)

        t_config = config_class.from_pretrained(args.teacher_model_path)
        #assert t_config.num_hidden_layers % int(layer_or_sparsity_val) == 0
        add_kwargs_to_config(
            t_config,
            num_relation_heads=args.num_relation_heads,
            num_labels=num_labels,
        #    layer_skip=int(t_config.num_hidden_layers / int(layer_or_sparsity_val)),
        )
        t_model = model_class.from_pretrained(
            args.teacher_model_path,
            config=t_config,
        )
        t_model = t_model.to(device)
        if is_dist:
            t_model = DistributedDataParallel(t_model, device_ids=[args.local_rank], output_device=args.local_rank)

        if not os.path.isdir(args.layer_or_sparsity):
            args.student_model_path = args.teacher_model_path
            
            layer_or_sparsity_key = args.layer_or_sparsity[-1]
            layer_or_sparsity_val = args.layer_or_sparsity[:-1]

            s_config = config_class.from_pretrained(args.student_model_path)
            if layer_or_sparsity_key == "L":
                if "enct5" in args.model_type:
                    add_kwargs_to_config(
                        s_config,
                        num_relation_heads=args.num_relation_heads,
                        num_labels=num_labels,
                        num_layers=int(layer_or_sparsity_val),
                    #    layer_skip=1,
                    )
                else:
                    add_kwargs_to_config(
                        s_config,
                        num_relation_heads=args.num_relation_heads,
                        num_labels=num_labels,
                        num_hidden_layers=int(layer_or_sparsity_val),
                    #    layer_skip=1,
                    )
                s_model = model_class.from_pretrained(
                    args.student_model_path,
                    config=s_config,
                )
                s_model = s_model.to(device)
                # TODO: MoE and other variants.
                #s_model.bert.moefy("4,8")
            else:
                #assert s_config.num_hidden_layers % 2 == 0
                add_kwargs_to_config(
                    s_config,
                    num_relation_heads=args.num_relation_heads,
                    num_labels=num_labels,
                #    layer_skip=int(t_config.num_hidden_layers / 2),
                )
                s_model = model_class.from_pretrained(
                    args.student_model_path,
                    config=s_config,
                )
                s_model = s_model.to(device)
                if "enct5" in args.model_type:
                    enc_model = getattr(s_model, "encoder", s_model)
                    enc_model.sparsify(layer_or_sparsity_val)
                    enc_model.densify()
                    dec_model = getattr(s_model, "decoder", s_model)
                    dec_model.sparsify(layer_or_sparsity_val)
                    dec_model.densify()
                else:
                    base_model = getattr(s_model, s_model.base_model_prefix, s_model)
                    base_model.sparsify(layer_or_sparsity_val)
                    base_model.densify()
                s_config.sparsity = layer_or_sparsity_val
                truncated_sparsity_map = {s: s_config.sparsity_map[s] for s in s_config.sparsity_map if int(s) >= int(layer_or_sparsity_val)}
                s_config.sparsity_map = truncated_sparsity_map
        else:
            args.student_model_path = args.layer_or_sparsity
            s_config = config_class.from_pretrained(args.student_model_path)
            add_kwargs_to_config(
                s_config,
                num_relation_heads=args.num_relation_heads,
                num_labels=num_labels,
            )
            s_model = model_class.from_pretrained(
                args.student_model_path,
                config=s_config,
            )
            s_model = s_model.to(device)
        if is_dist:
            s_model = DistributedDataParallel(s_model, device_ids=[args.local_rank], output_device=args.local_rank)

        # Optimizer, split weights in two groups, one with weight decay and the other not.
        no_decay = ["bias", "LayerNorm.weight"]
        grouped_parameters = [
            {
                "params": [p for n, p in s_model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": args.weight_decay,
            },
            {
                "params": [p for n, p in s_model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]
        optimizer = AdamW(grouped_parameters, lr=args.learning_rate)

        # NOTE: the training loader needs to be prepared before we grab his length below (cause its length will be
        # shorter in multiprocess).

        # Scheduler and math around the number of training steps.
        num_update_steps_per_epoch = math.ceil(len(train_loader) / args.num_grad_accum_steps)
        num_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        num_patience_steps = args.num_patience_epochs * num_update_steps_per_epoch
        num_warmup_steps = int(num_train_steps * args.warmup_proportion)

        lr_scheduler = get_scheduler(
            name=args.lr_scheduler_type,
            optimizer=optimizer,
            num_warmup_steps=num_warmup_steps,
            num_training_steps=num_train_steps,
        )

        # Train!
        total_batch_size = args.per_device_train_batch_size * args.num_grad_accum_steps
        if is_dist:
            total_batch_size = total_batch_size * dist.get_world_size()

        logger.info(f"***** Running training *****")
        logger.info(f"  Num examples = {len(train_dataset)}")
        logger.info(f"  Num epochs = {args.num_train_epochs}")
        logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
        logger.info(f"  Total train batch size (w. accumulation, parallel & distributed) = {total_batch_size}")
        logger.info(f"  Gradient accumulation steps = {args.num_grad_accum_steps}")
        logger.info(f"  Total optimization steps = {num_train_steps}")
        # Only show the progress bar once on each machine.
        progress_bar = tqdm(range(num_train_steps), disable=not is_main)
        num_completed_steps = 0
        train_losses = AverageMeter(args.num_grad_accum_steps)
        best_dev_step = 0
        best_dev_path = ""
        best_dev_metric = {}

        if is_fp16:
            scaler = amp.GradScaler()

        t_model.eval()
        for epoch in range(args.num_train_epochs):
            for step, batch in enumerate(train_loader):
                s_model.train()
                batch = [v.to(device) for k, v in batch._asdict().items()]
                if is_fp16:
                    with amp.autocast():
                        with torch.no_grad():
                            t_output = t_model(batch)
                else:
                    with torch.no_grad():
                        t_output = t_model(batch)
                if is_fp16:
                    with amp.autocast():
                        s_output = s_model(batch)
                        loss = model_class.loss_fn(t_output, s_output)
                else:
                    s_output = s_model(batch)
                    loss = model_class.loss_fn(t_output, s_output)
                train_losses.update(loss.item())
                loss = loss / args.num_grad_accum_steps
                if is_fp16:
                    scaler.scale(loss).backward()
                else:
                    loss.backward()
                if step % args.num_grad_accum_steps == 0 or step == len(train_loader) - 1:
                    if is_fp16:
                        scaler.unscale_(optimizer)
                        torch.nn.utils.clip_grad_norm_(s_model.parameters(), args.max_grad_norm)
                        scaler.step(optimizer) # Will check whether the gradients are unscaled or not before stepping.
                        scaler.update()
                    else:
                        torch.nn.utils.clip_grad_norm_(s_model.parameters(), args.max_grad_norm)
                        optimizer.step()
                    lr_scheduler.step()
                    s_model.zero_grad()
                    progress_bar.update(1)
                    num_completed_steps += 1
                
                if num_completed_steps % args.log_interval == 0:
                    logger.info("***** Running evaluation *****")
                    logger.info(f"  Num completed epochs = {epoch}")
                    logger.info(f"  Num completed steps = {num_completed_steps}")
                    s_model.eval()
                    preds, labels = [], []
                    with torch.no_grad():
                        for batch in dev_loader:
                            batch = [v.to(device) for k, v in batch._asdict().items()]
                            s_output = s_model(batch)
                            pred, label = s_output.prediction, s_output.label
                            if is_dist:
                                preds.extend(gather(pred).cpu().numpy().tolist())
                                labels.extend(gather(label).cpu().numpy().tolist())
                            else:
                                preds.extend(pred.cpu().numpy().tolist())
                                labels.extend(label.cpu().numpy().tolist())
                    
                    dev_metric = metric_fn(preds, labels)
                    logger.info(f"  Dev metric = {dev_metric}")

                    if not best_dev_metric or dev_metric[args.selection_metric] > best_dev_metric[args.selection_metric]:
                        logger.info("***** Saving best *****")
                        best_dev_step = num_completed_steps
                        best_dev_metric.update(**dev_metric)
                        
                        best_dev_path = os.path.join(args.output_dir, "ckpt_iter_x")
                        if is_main:
                            tokenizer.save_pretrained(best_dev_path)
                            if is_dist:
                                model_to_save = s_model.module
                            else:
                                model_to_save = s_model
                            model_to_save.save_pretrained(best_dev_path)
                            s_config.save_pretrained(best_dev_path)

                if num_completed_steps - best_dev_step >= num_patience_steps:
                    logger.info("***** Early stopping *****")
                    break
            # If early stop, then break the outer loop.
            else:
                continue
            break          

        logger.info(f"***** Finalizing training *****")
        logger.info(f"  Best dev step = {best_dev_step}")
        logger.info(f"  Best dev metric = {best_dev_metric}")

    # Test is only conducted in the main process.
    if args.do_test and is_main:
        try:
            model_path = best_dev_path
        except:
            model_path = args.student_model_path

        # Load pretrained tokenizer with necessary resizing.
        tokenizer = tokenizer_class.from_pretrained(model_path)
        
        # Data pipeline.
        data_pipeline = pipeline_class(tokenizer, label_map, args.max_length)

        test_examples = data_reader.get_test_examples()
        test_examples = data_pipeline.build(test_examples, template=args.template, verbalizer=args.verbalizer)
        
        test_dataset = Dataset(test_examples, shuffle=False)
        test_loader = DataLoader(test_dataset, batch_size=args.per_device_eval_batch_size, collate_fn=data_pipeline.collate)
        
        config = config_class.from_pretrained(model_path)
        model = model_class.from_pretrained(
            model_path,
            config=config,
        )
        model = model.to(device)
            
        # Test!
        logger.info("***** Running testing *****")
        model.eval()
        with torch.no_grad():
            preds, labels = [], []
            for batch in test_loader:
                batch = [v.to(device) for k, v in batch._asdict().items()]
                output = model(batch)
                pred, label = output.prediction, output.label
                preds.extend(pred.cpu().numpy().tolist())
                labels.extend(label.cpu().numpy().tolist())

        test_metric = metric_fn(preds, labels)
        logger.info("***** Finalizing testing *****") 
        logger.info(f"  Test metric = {test_metric}")

    # Infer.
    if args.do_infer and is_main:
        try:
            model_path = best_dev_path
        except:
            model_path = args.student_model_path

        # Load pretrained tokenizer with necessary resizing.
        tokenizer = tokenizer_class.from_pretrained(model_path)
        
        # Data pipeline.
        data_pipeline = pipeline_class(tokenizer, label_map, args.max_length)

        test_examples = data_reader.get_test_examples()
        test_examples = data_pipeline.build(test_examples, template=args.template, verbalizer=args.verbalizer)
        
        test_dataset = Dataset(test_examples, shuffle=False)
        test_loader = DataLoader(test_dataset, batch_size=args.per_device_eval_batch_size, collate_fn=data_pipeline.collate)
        
        config = config_class.from_pretrained(model_path)
        model = model_class.from_pretrained(
            model_path,
            config=config,
        )
        model = model.to(device)

        # Infer!
        logger.info("***** Running inference *****")
        model.eval()
        with torch.no_grad():
            preds = []
            for batch in test_loader:
                batch = [v.to(device) for k, v in batch._asdict().items()]
                output = model(batch)
                pred = output.prediction
                preds.extend(pred.cpu().numpy().tolist())

        logger.info("***** Finalizing inference *****")
        save_path = os.path.join(args.output_dir, f"{name_map[args.task_name]}.tsv")
        with open(save_path, "w") as fo:
            writer = csv.writer(fo, delimiter="\t")
            writer.writerow(["idx", "label"])
            for idx, pred in enumerate(preds):
                writer.writerow([idx, reverse_label_map(pred)])
    

if __name__ == "__main__":
    """
    1. Single-Node multi-process distributed training

    ::

        >>> python -m torch.distributed.launch --nproc_per_node=NUM_GPUS_YOU_HAVE
                YOUR_TRAINING_SCRIPT.py (--arg1 --arg2 --arg3 and all other
                arguments of your training script)

    2. Multi-Node multi-process distributed training: (e.g. two nodes)


    Node 1: *(IP: 192.168.1.1, and has a free port: 1234)*

    ::

        >>> python -m torch.distributed.launch --nproc_per_node=NUM_GPUS_YOU_HAVE
                --nnodes=2 --node_rank=0 --master_addr="192.168.1.1"
                --master_port=1234 YOUR_TRAINING_SCRIPT.py (--arg1 --arg2 --arg3
                and all other arguments of your training script)

    Node 2:

    ::

        >>> python -m torch.distributed.launch --nproc_per_node=NUM_GPUS_YOU_HAVE
                --nnodes=2 --node_rank=1 --master_addr="192.168.1.1"
                --master_port=1234 YOUR_TRAINING_SCRIPT.py (--arg1 --arg2 --arg3
                and all other arguments of your training script)
    """
    main()
