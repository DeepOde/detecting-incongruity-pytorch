# -*- coding: utf-8 -*-

import argparse
import os
from pathlib import Path
import pickle
import time
import datetime

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from sklearn.metrics import roc_auc_score

import data
from model import AttnHrDualEncoderModel
import utils

import csv  # To write test results to a csv file.

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


def evaluate(model, data, save_preds = False):
    model.eval()

    with torch.no_grad():
        criterion = nn.BCEWithLogitsLoss()

        batch_size = 64
        val_dataloader = DataLoader(data, batch_size=batch_size)

        loss_sum = 0
        preds_all = []
        labels_all = []
        for batch in val_dataloader:
            headlines, headline_lengths, bodys, para_lengths, labels = tuple(b.to(device) for b in batch)

            preds = model(headlines, headline_lengths, bodys, para_lengths)

            loss = criterion(preds, labels)
            loss_sum += len(labels) * loss

            preds_all.append(preds)
            labels_all.append(labels)

        preds_all = torch.cat(preds_all)
        labels_all = torch.cat(labels_all)

        if save_preds:
            print("Saving predictions to 'test_preds.txt'...")
            if device == 'cpu':
                preds_all_np, labels_all_np = preds_all.numpy(), labels_all.numpy()
            else:
                preds_all_np, labels_all_np = preds_all.detach().cpu().numpy(), labels_all.detach().cpu().numpy()

                print("Shapes:",preds_all_np.shape,labels_all_np.shape)

                with open('test_preds.csv', 'w', newline='') as file:
                    f = csv.writer(file, delimiter=',')

                    for i in range(len(preds_all_np)):
                        f.writerow([str(i), labels_all_np[i], preds_all_np[i]])
                        
                    
                

        num_correct = torch.eq((preds_all > 0).int(), labels_all.int()).sum().item()
        num_total = len(preds_all)

        loss = loss_sum / num_total
        acc = num_correct / num_total
        auc = roc_auc_score(labels_all.tolist(), preds_all.tolist())

        return loss, acc, auc


def train(model, train_data, val_data, args):
    model.train()

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    criterion = nn.BCEWithLogitsLoss()

    num_iterations_per_epoch = len(train_data) / args.batch_size
    val_eval_freq = int(args.val_evaluation_freq * num_iterations_per_epoch)
    print(f"Val set evaluated every {val_eval_freq:,} steps (approx. {args.val_evaluation_freq} epoch)")

    es = utils.EarlyStopping(args.early_stopping_patience)
    initial_time = time.time()

    train_dataloader = DataLoader(train_data, batch_size=args.batch_size, shuffle=True)

    global_step = 0
    epoch_no = 0
    while True:
        print(f"EPOCH #{epoch_no+1}")

        # Train single epoch
        for batch in train_dataloader:
            headlines, headline_lengths, bodys, para_lengths, labels = tuple(b.to(device) for b in batch)
            optimizer.zero_grad()

            preds = model(headlines, headline_lengths, bodys, para_lengths)
            loss = criterion(preds, labels)

            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
            optimizer.step()
            
            global_step += 1
            print("globstep:", global_step)
            if global_step % val_eval_freq == 0:
                # Evaluate on validation set
                val_loss, val_acc, val_auc = evaluate(model, val_data)
                model.train()

                end_time = time.time()
                minutes_elapsed = int((end_time - initial_time)/60)
                print("STEP: {:7} | TIME: {:4}min | VAL LOSS: {:.4f} | VAL ACC: {:.4f} | VAL AUROC: {:.4f}".format(
                    global_step, minutes_elapsed, val_loss, val_acc, val_auc
                ))

                # Check early stopping
                if global_step >= args.min_iterations:
                    es.record_loss(val_loss, model)

                if es.should_stop():
                    print(f"Early stopping at STEP: {global_step}...")
                    return

            if global_step == args.max_iterations:
                print(f"Stopping after reaching max iterations({global_step})...")
                return
        epoch_no += 1


def main():
    print(f"Using device: {device}")

    parser = argparse.ArgumentParser()

    # dataset-related params
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--max-headline-len", type=int, required=True)
    parser.add_argument("--max-para-len", type=int, required=True)
    parser.add_argument("--max-num-para", type=int, required=True)
    parser.add_argument("--cache-dataset", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=Path("/tmp"))

    # model-related params
    parser.add_argument("--headline-rnn-hidden-dim", type=int, required=True)
    parser.add_argument("--word-level-rnn-hidden-dim", type=int, required=True)
    parser.add_argument("--paragraph-level-rnn-hidden-dim", type=int, required=True)

    # training-related params
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--min-iterations", type=int, default=2000)
    parser.add_argument("--max-iterations", type=int, default=100000)
    parser.add_argument("--val-evaluation-freq", type=float, default=0.1, 
                        help="validation loss/acc evaluation frequency in terms of fraction of epoch")
    parser.add_argument("--early-stopping-patience", type=int, default=10,
                        help="number of times to wait for lower val loss before early stopping")
    parser.add_argument("--evaluate-test-after-train", action="store_true",
                        help="evaluate on test set after training")

    args = parser.parse_args()

    # Load data
    print("Loading train dataset...")
    train_dataset = data.load_dataset(args.data_dir, 'train', args.max_headline_len, 
                                      args.max_para_len, args.max_num_para, args.cache_dataset, 
                                      args.cache_dir, max_articles=None)
    print(f"Train dataset size: {len(train_dataset):9,}")
    print("Loading val dataset...")
    val_dataset = data.load_dataset(args.data_dir, 'dev', args.max_headline_len, 
                                    args.max_para_len, args.max_num_para, args.cache_dataset, 
                                    args.cache_dir)
    print(f"Val dataset size: {len(val_dataset):9,}")

    glove_embeds = data.load_glove(args.data_dir)

    # Initialize model
    vocab_size, embedding_dim = glove_embeds.shape

    print("Initializing model...")
    hidden_dims = {
        'headline': args.headline_rnn_hidden_dim,
        'word': args.word_level_rnn_hidden_dim,
        'paragraph': args.word_level_rnn_hidden_dim
    }
    model = AttnHrDualEncoderModel(hidden_dims, vocab_size, embedding_dim, 
                                   pretrained_embeds=glove_embeds)
    model.to(device)
    print("Initialization done!")

    # Train
    train(model, train_dataset, val_dataset, args)

    # Evaluate test
    if args.evaluate_test_after_train:
        print("Loading test dataset...")
        test_dataset = data.load_dataset(args.data_dir, 'test', args.max_headline_len,
                                         args.max_para_len, args.max_num_para, args.cache_dataset, 
                                         args.cache_dir)
        print(f"Test dataset size: {len(test_dataset):9,}")

        if utils.CHECKPOINT_SAVE_PATH.exists():
            print(f"Loading model checkpoint with min val loss...")
            model.load_state_dict(torch.load(utils.CHECKPOINT_SAVE_PATH))
            print(f"Loading done!")

        _, test_acc, test_auc = evaluate(model, test_dataset, save_preds = True)

        print(f"TEST ACC: {test_acc:.4f} | TEST AUROC: {test_auc:.4f}")

        if utils.CHECKPOINT_SAVE_PATH.exists():
            utils.CHECKPOINT_SAVE_PATH.unlink()  # delete model checkpoint
            
        # result logging
        with open('./TEST_run_result.txt', 'a') as f:
            f.write(
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + '\t' + \
                    str('t_accr:\t') + str(test_acc) + '\t' + \
                    str('t_auroc:\t') + str(test_auc) + '\t' + \
                    '\n')


if __name__ == "__main__":
    main()
