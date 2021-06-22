import os
import torch
import numpy as np
import pickle
import json
from pprint import pprint

from .dataloader import get_loaders
from .optimizer import get_optimizer
from .scheduler import get_scheduler
from .criterion import get_criterion
from .metric import get_metric
from .custom_model import *
from .model import *

import wandb


def run(args, train_data, valid_data):
    train_loader, valid_loader = get_loaders(args, train_data, valid_data)

    # only when using warmup scheduler
    args.total_steps = int(len(train_loader.dataset) / args.batch_size) * (
        args.n_epochs
    )
    args.warmup_steps = args.total_steps // 10

    model = get_model(args)
    optimizer = get_optimizer(model, args)
    scheduler = get_scheduler(optimizer, args)

    best_auc = -1
    best_acc = -1
    early_stopping_counter = 0

    for epoch in range(args.n_epochs):
        print(f"Start Training: Epoch {epoch + 1}")

        ### TRAIN
        train_auc, train_acc, train_loss = train(train_loader, model, optimizer, args)

        ### VALID
        auc, acc, _, _ = validate(valid_loader, model, args)

        try:
            fold_str = f"fold{args.fold}-"
        except:
            fold_str = ""
        wandb.log(
            {
                "epoch": epoch,
                f"{fold_str}train_loss": train_loss,
                f"{fold_str}train_auc": train_auc,
                f"{fold_str}train_acc": train_acc,
                f"{fold_str}valid_auc": auc,
                f"{fold_str}valid_acc": acc,
            }
        )
        
        ### TODO: model save or early stopping
        if auc > best_auc:
            best_epoch_auc = epoch  # best_auc가 출현한 epoch
            best_auc = auc
            best_acc = acc
            # torch.nn.DataParallel로 감싸진 경우 원래의 model을 가져옵니다.
            model_to_save = model.module if hasattr(model, "module") else model
            save_checkpoint(
                {
                    "epoch": epoch + 1,
                    "state_dict": model_to_save.state_dict(),
                },
                args.model_dir,
                "model.pt",
            )
            early_stopping_counter = 0
        else:
            early_stopping_counter += 1
            if early_stopping_counter >= args.patience:
                print(
                    f"EarlyStopping counter: {early_stopping_counter} out of {args.patience}"
                )
                break

        # scheduler
        if args.scheduler == "plateau":
            scheduler.step(auc)
        else:
            scheduler.step()

    # best epoch과 best score를 저장해 놓은 best_dict
    best_dict = {
        "best_epoch(auc)": best_epoch_auc,
        "auc": best_auc,
        "acc": best_acc,
    }

    # Save best_dict
    with open(f"{args.model_dir}/best_dict.json", "w") as fw:
        json.dump(best_dict, fw, indent=4)


def train(train_loader, model, optimizer, args):
    model.train()

    total_preds = []
    total_targets = []
    losses = []
    for step, batch in enumerate(train_loader):
        input = process_batch(batch, args)
        preds = model(input)
        targets = input[-1] 

        loss = compute_loss(preds, targets)
        update_params(loss, model, optimizer, args)

        if step % args.log_steps == 0:
            print(f"Training steps: {step} Loss: {str(loss.item())}")
            
        # predictions
        preds = preds[:, -1]
        targets = targets[:, -1]
        
        if args.device == "cuda":
            preds = preds.to("cpu").detach().numpy()
            targets = targets.to("cpu").detach().numpy()
        else:  # cpu
            preds = preds.detach().numpy()
            targets = targets.detach().numpy()

        total_preds.append(preds)
        total_targets.append(targets)
        losses.append(loss)
    
    total_preds = np.concatenate(total_preds)
    total_targets = np.concatenate(total_targets)

    # Train AUC / ACC
    auc, acc = get_metric(total_targets, total_preds)
    loss_avg = sum(losses) / len(losses)
    print(f"TRAIN AUC : {auc} ACC : {acc}")
    return auc, acc, loss_avg


def validate(valid_loader, model, args):
    model.eval()

    total_preds = []
    total_targets = []
    for step, batch in enumerate(valid_loader):
        input = process_batch(batch, args)
        preds = model(input)
        targets = input[-1]  # correct

        # predictions
        preds = preds[:, -1]
        targets = targets[:, -1]

        if args.device == "cuda":
            preds = preds.to("cpu").detach().numpy()
            targets = targets.to("cpu").detach().numpy()
        else:  # cpu
            preds = preds.detach().numpy()
            targets = targets.detach().numpy()

        total_preds.append(preds)
        total_targets.append(targets)

    total_preds = np.concatenate(total_preds)
    total_targets = np.concatenate(total_targets)

    # Train AUC / ACC
    auc, acc = get_metric(total_targets, total_preds)

    print(f"VALID AUC : {auc} ACC : {acc}\n")

    return auc, acc, total_preds, total_targets


def inference(args, test_data):

    model = load_model(args)
    model.eval()

    _, test_loader = get_loaders(args, None, test_data)

    total_preds = []

    for batch in test_loader:
        input = process_batch(batch, args)

        preds = model(input)

        # predictions
        preds = preds[:, -1]

        if args.device == "cuda":
            preds = preds.to("cpu").detach().numpy()
        else:  # cpu
            preds = preds.detach().numpy()

        total_preds += list(preds)

    try:
        write_path = os.path.join(
            args.output_dir, f"{args.wandb_run_name}-output_{args.fold}.csv"
        )
    except:
        write_path = os.path.join(args.output_dir, f"{args.wandb_run_name}-output.csv")

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
    with open(write_path, "w", encoding="utf8") as w:
        print("writing prediction : {}".format(write_path))
        w.write("id,prediction\n")
        for id, p in enumerate(total_preds):
            w.write("{},{}\n".format(id, p))


def get_model(args):
    """
    Load model and move tensors to a given devices.
    """
    model = None
    if args.model == "lstm":
        model = LSTM(args)
    if args.model == "lstmattn":
        model = LSTMATTN(args)
    if args.model == "gruattn":
        model = GRUATTN(args)
    if args.model == "addgruattn":
        model = ADDGRUATTN(args)
    if args.model == "attngru":
        model = ATTNGRU(args)
    if args.model == "saint":
        model = Saint(args)
    if args.model == "saintcustom":
        model = Saint_custom(args)
    if args.model == "bert":
        model = Bert(args)
    if args.model == "lastquery":
        model = LastQuery(args)
    if args.model == "cnn":
        model = BaseConv(args)
    if args.model == "deepcnn":
        model = DeepConv(args)
	
    if not model:
        raise RuntimeError(
            f"Model {args.model} not defined: choose one of: lstm, lstmattn, bert"
        )

    model.to(args.device)

    return model


# 배치 전처리
def process_batch(batch, args):

    correct, mask = batch[-2], batch[-1]

    # change to float
    correct = correct.type(torch.FloatTensor)
    mask = mask.type(torch.FloatTensor)

    #  interaction을 임시적으로 correct를 한칸 우측으로 이동한 것으로 사용
    #    saint의 경우 decoder에 들어가는 input이다
    interaction = correct + 1  # 패딩을 위해 correct값에 1을 더해준다.
    interaction = interaction.roll(shifts=1, dims=1)
    interaction_mask = mask.roll(shifts=1, dims=1)
    interaction_mask[:, 0] = 0
    interaction = (interaction * interaction_mask)

    cate_batch = [((b + 1) * mask).to(torch.int64).to(args.device) for b in batch[:args.n_cates-1]]
    cate_batch.append(interaction.to(torch.int64).to(args.device))

    cont_batch = [((b) * mask).to(torch.float32).to(args.device) for b in batch[args.n_cates-1:-2]]

    processed_batch = [cate_batch, cont_batch]
    processed_batch.append(mask.to(args.device))
    processed_batch.append(correct.to(args.device))

    return tuple(processed_batch)


# loss계산하고 parameter update!
def compute_loss(preds, targets):
    """
    Args :
        preds   : (batch_size, max_seq_len)
        targets : (batch_size, max_seq_len)

    """
    loss = get_criterion(preds, targets)
    # 마지막 시퀀드에 대한 값만 loss 계산
    loss = loss[:, -1]
    loss = torch.mean(loss)
    return loss


def update_params(loss, model, optimizer, args):
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad)
    optimizer.step()
    optimizer.zero_grad()


def save_checkpoint(state, model_dir, model_filename):
    print("saving model ...")
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)
    torch.save(state, os.path.join(model_dir, model_filename))


def load_model(args):

    model_path = os.path.join(args.model_dir, args.model_name)
    print("Loading Model from:", model_path)
    load_state = torch.load(model_path)
    model = get_model(args)

    # 1. load model state
    model.load_state_dict(load_state["state_dict"], strict=True)

    print("Loading Model from:", model_path, "...Finished.")
    return model
