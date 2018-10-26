# -*- coding: utf-8 -*-
"""
Python File Template 
"""
import json
import os
import sys
import argparse

import logging
import numpy as np
import time
import torchtext
from torch.autograd import Variable
from torch.optim import Adam
from torch.utils.data import DataLoader

import config
import evaluate
import utils
import copy
import random

import torch
import torch.nn as nn
from torch import cuda

from beam_search import SequenceGenerator
from evaluate import evaluate_beam_search, get_match_result, self_redundancy, evaluate_nll_loss
from pykp.dataloader import KeyphraseDataLoader
from utils import Progbar, plot_learning_curve

import pykp
from pykp.io import KeyphraseDataset
from pykp.model import Seq2SeqLSTMAttention, Seq2SeqLSTMAttentionCascading

import time


def to_cpu_list(input):
    assert isinstance(input, list)
    output = [int(item.data.cpu().numpy()) for item in input]
    return output


def time_usage(func):
    # argnames = func.func_code.co_varnames[:func.func_code.co_argcount]
    fname = func.__name__

    def wrapper(*args, **kwargs):
        beg_ts = time.time()
        retval = func(*args, **kwargs)
        end_ts = time.time()
        print(fname, "elapsed time: %f" % (end_ts - beg_ts))
        return retval

    return wrapper


__author__ = "Rui Meng"
__email__ = "rui.meng@pitt.edu"


def to_np(x):
    if isinstance(x, float) or isinstance(x, int):
        return x
    if isinstance(x, np.ndarray):
        return x
    return x.data.cpu().numpy()


def orthogonal_penalty(_m, I, l_n_norm=2):
    # _m: h x n
    # I:  n x n
    m = torch.mm(torch.t(_m), _m)  # n x n
    return torch.norm((m - I), p=l_n_norm)


class ReplayMemory(object):

    def __init__(self, capacity=500):
        # vanilla replay memory
        self.capacity = capacity
        self.memory = []
        self.position = 0

    def push(self, stuff):
        """Saves a transition."""
        if len(self.memory) < self.capacity:
            self.memory.append(None)
        self.memory[self.position] = stuff
        self.position = (self.position + 1) % self.capacity

    def sample(self, batch_size):
        return random.sample(self.memory, batch_size)

    def __len__(self):
        return len(self.memory)


def random_insert(_list, elem):
    insert_before_this = np.random.randint(low=0, high=len(_list) + 1)
    return _list[:insert_before_this] + [elem] + _list[insert_before_this:], insert_before_this


def get_target_encoder_loss(model, source_representations, target_representations, input_trg_np, replay_memory, criterion, opt):
    # source_representations: batch x hid
    # target_representations: time x batch x hid
    # here, we use all target representations at sep positions, to do the classification task
    sep_id = opt.word2id[pykp.io.SEP_WORD]
    eos_id = opt.word2id[pykp.io.EOS_WORD]
    target_representations = target_representations.permute(1, 0, 2)  # batch x time x hid
    batch_size = target_representations.size(0)
    n_neg = opt.n_negative_samples
    coef = opt.target_encoder_lambda
    if coef == 0.0:
        return 0.0
    batch_inputs_source, batch_inputs_target, batch_labels = [], [], []
    source_representations = source_representations.detach()
    for b in range(batch_size):
        # 0. find sep positions
        inp_trg_np = input_trg_np[b]
        for i in range(len(inp_trg_np)):
            if inp_trg_np[i] in [sep_id, eos_id]:
                trg_rep = target_representations[b][i]
                # 1. negative sampling
                if len(replay_memory) >= n_neg:
                    neg_list = replay_memory.sample(n_neg)
                    inputs, which = random_insert(neg_list, source_representations[b])
                    inputs = torch.stack(inputs, 0)  # n_neg+1 x hid
                    batch_inputs_source.append(inputs)
                    batch_inputs_target.append(trg_rep)
                    batch_labels.append(which)
        # 2. push source representations into replay memory
        replay_memory.push(source_representations[b])
    if len(batch_inputs_source) == 0:
        return 0.0
    batch_inputs_source = torch.stack(
        batch_inputs_source, 0)  # batch x n_neg+1 x hid
    batch_inputs_target = torch.stack(batch_inputs_target, 0)  # batch x hid
    batch_labels = np.array(batch_labels)  # batch
    batch_labels = torch.autograd.Variable(
        torch.from_numpy(batch_labels).type(torch.LongTensor))
    if torch.cuda.is_available():
        batch_labels = batch_labels.cuda()

    # 3. prediction
    batch_inputs_target = model.target_encoding_mlp(
        batch_inputs_target)[-1]  # last layer, batch x mlp_hid
    batch_inputs_target = torch.stack(
        [batch_inputs_target] * batch_inputs_source.size(1), 1)
    pred = model.bilinear_layer(
        batch_inputs_source, batch_inputs_target).squeeze(-1)  # batch x n_neg+1
    pred = torch.nn.functional.log_softmax(pred, dim=-1)  # batch x n_neg+1
    # 4. backprop & update
    loss = criterion(pred, batch_labels)
    loss = loss * coef
    return loss


def get_orthogonal_penalty(trg_copy_target_np, decoder_outputs, opt):
    orth_coef = opt.orthogonal_regularization_lambda
    if orth_coef == 0:
        return 0.0
    orth_position = opt.orthogonal_regularization_position
    # aux loss: make the decoder outputs at all <SEP>s to be orthogonal
    sep_id = opt.word2id[pykp.io.SEP_WORD]
    penalties = []
    for i in range(len(trg_copy_target_np)):
        seps = []
        for j in range(len(trg_copy_target_np[i])):  # len of target
            if orth_position == "sep":
                if trg_copy_target_np[i][j] == sep_id:
                    seps.append(decoder_outputs[i][j])
            elif orth_position == "post":
                if j == 0:
                    continue
                if trg_copy_target_np[i][j - 1] == sep_id:
                    seps.append(decoder_outputs[i][j])
        if len(seps) > 1:
            seps = torch.stack(seps, -1)  # h x n
            identity = torch.eye(seps.size(-1))  # n x n
            if torch.cuda.is_available():
                identity = identity.cuda()
            penalty = orthogonal_penalty(seps, identity, 2)  # 1
            penalties.append(penalty)

    if len(penalties) > 0 and decoder_outputs.size(0) > 0:
        penalties = torch.sum(torch.stack(penalties, -1)) / \
            float(decoder_outputs.size(0))
    else:
        penalties = 0.0
    penalties = penalties * orth_coef
    return penalties


def train_ml(one2one_batch, model, optimizer, criterion, replay_memory, opt):
    src, src_len, trg, trg_target, trg_copy_target, src_oov, oov_lists = one2one_batch
    max_oov_number = max([len(oov) for oov in oov_lists])
    trg_copy_target_np = copy.copy(trg_copy_target)
    trg_copy_np = copy.copy(trg)

    print("src size - ", src.size())
    print("target size - ", trg.size())

    optimizer.zero_grad()
    if torch.cuda.is_available():
        src = src.cuda()
        trg = trg.cuda()
        trg_target = trg_target.cuda()
        trg_copy_target = trg_copy_target.cuda()
        src_oov = src_oov.cuda()

    decoder_log_probs, decoder_outputs, _, source_representations, target_representations = model.forward(src, src_len, trg, src_oov, oov_lists)

    te_loss = get_target_encoder_loss(model, source_representations, target_representations, trg_copy_np, replay_memory, criterion, opt)
    penalties = get_orthogonal_penalty(trg_copy_target_np, decoder_outputs, opt)
    if opt.orth_reg_mode == 1:
        penalties = penalties + get_orthogonal_penalty(trg_copy_target_np, target_representations.permute(1, 0, 2), opt)

    # simply average losses of all the predicitons
    # IMPORTANT, must use logits instead of probs to compute the loss,
    # otherwise it's super super slow at the beginning (grads of probs are
    # small)!
    start_time = time.time()

    if not opt.copy_attention:
        nll_loss = criterion(
            decoder_log_probs.contiguous().view(-1, opt.vocab_size),
            trg_target.contiguous().view(-1)
        )
    else:
        nll_loss = criterion(
            decoder_log_probs.contiguous().view(-1, opt.vocab_size + max_oov_number),
            trg_copy_target.contiguous().view(-1)
        )
    nll_loss = nll_loss * (1 - opt.loss_scale)
    print("--loss calculation- %s seconds ---" % (time.time() - start_time))
    loss = nll_loss + penalties + te_loss

    start_time = time.time()
    loss.backward(retain_graph=True)
    print("--backward- %s seconds ---" % (time.time() - start_time))

    if opt.max_grad_norm > 0:
        pre_norm = torch.nn.utils.clip_grad_norm(
            model.parameters(), opt.max_grad_norm)
        after_norm = (sum([p.grad.data.norm(
            2) ** 2 for p in model.parameters() if p.grad is not None])) ** (1.0 / 2)
        # logging.info('clip grad (%f -> %f)' % (pre_norm, after_norm))
    optimizer.step()

    return to_np(loss), decoder_log_probs, to_np(nll_loss), to_np(penalties), to_np(te_loss)


def brief_report(epoch, batch_i, one2one_batch, loss_ml, decoder_log_probs, opt):
    logging.info(
        '======================  %d  =========================' % (batch_i))

    logging.info('Epoch : %d Minibatch : %d, Loss=%.5f' %
                 (epoch, batch_i, np.mean(loss_ml)))
    sampled_size = 2
    logging.info(
        'Printing predictions on %d sampled examples by greedy search' % sampled_size)

    src, _, trg, trg_target, trg_copy_target, src_ext, oov_lists = one2one_batch
    if torch.cuda.is_available():
        src = src.data.cpu().numpy()
        decoder_log_probs = decoder_log_probs.data.cpu().numpy()
        max_words_pred = decoder_log_probs.argmax(axis=-1)
        trg_target = trg_target.data.cpu().numpy()
        trg_copy_target = trg_copy_target.data.cpu().numpy()
    else:
        src = src.data.numpy()
        decoder_log_probs = decoder_log_probs.data.numpy()
        max_words_pred = decoder_log_probs.argmax(axis=-1)
        trg_target = trg_target.data.numpy()
        trg_copy_target = trg_copy_target.data.numpy()

    sampled_trg_idx = np.random.random_integers(
        low=0, high=len(trg) - 1, size=sampled_size)
    src = src[sampled_trg_idx]
    oov_lists = [oov_lists[i] for i in sampled_trg_idx]
    max_words_pred = [max_words_pred[i] for i in sampled_trg_idx]
    decoder_log_probs = decoder_log_probs[sampled_trg_idx]
    if not opt.copy_attention:
        trg_target = [trg_target[i] for i in
                      sampled_trg_idx]  # use the real target trg_loss (the starting <BOS> has been removed and contains oov ground-truth)
    else:
        trg_target = [trg_copy_target[i] for i in sampled_trg_idx]

    for i, (src_wi, pred_wi, trg_i, oov_i) in enumerate(
            zip(src, max_words_pred, trg_target, oov_lists)):
        nll_prob = -np.sum([decoder_log_probs[i][l][pred_wi[l]]
                            for l in range(len(trg_i))])
        find_copy = np.any([x >= opt.vocab_size for x in src_wi])
        has_copy = np.any([x >= opt.vocab_size for x in trg_i])

        sentence_source = [opt.id2word[x] if x < opt.vocab_size else oov_i[x - opt.vocab_size] for x in
                           src_wi]
        sentence_pred = [opt.id2word[x] if x < opt.vocab_size else oov_i[x - opt.vocab_size] for x in
                         pred_wi]
        sentence_real = [opt.id2word[x] if x < opt.vocab_size else oov_i[x - opt.vocab_size] for x in
                         trg_i]

        sentence_source = sentence_source[:sentence_source.index(
            '<pad>')] if '<pad>' in sentence_source else sentence_source
        sentence_pred = sentence_pred[
            :sentence_pred.index('<pad>')] if '<pad>' in sentence_pred else sentence_pred
        sentence_real = sentence_real[
            :sentence_real.index('<pad>')] if '<pad>' in sentence_real else sentence_real

        logging.info('==================================================')
        logging.info('Source: %s ' % (' '.join(sentence_source)))
        logging.info('\t\tPred : %s (%.4f)' % (' '.join(sentence_pred), nll_prob) + (
            ' [FIND COPY]' if find_copy else ''))
        logging.info('\t\tReal : %s ' % (' '.join(sentence_real)) + (
            ' [HAS COPY]' + str(trg_i) if has_copy else ''))


def train_model(model, optimizer_ml, optimizer_rl, criterion, train_data_loader, valid_data_loader, test_data_loader, opt):
    generator = SequenceGenerator(model,
                                  eos_id=opt.word2id[pykp.io.EOS_WORD],
                                  bos_id=opt.word2id[pykp.io.BOS_WORD],
                                  sep_id=opt.word2id[pykp.io.SEP_WORD],
                                  beam_size=opt.beam_size,
                                  max_sequence_length=opt.max_sent_length
                                  )

    logging.info(
        '======================  Checking GPU Availability  =========================')
    if torch.cuda.is_available():
        if isinstance(opt.gpuid, int):
            opt.gpuid = [opt.gpuid]
        logging.info('Running on GPU! devices=%s' % str(opt.gpuid))
        # model = nn.DataParallel(model, device_ids=opt.gpuid)
    else:
        logging.info('Running on CPU!')

    logging.info(
        '======================  Start Training  =========================')

    checkpoint_names = []
    train_ml_history_losses = []
    valid_history_losses = []
    test_history_losses = []
    # best_loss = sys.float_info.max # for normal training/testing loss
    # (likelihood)
    best_loss = sys.float_info.max  # for f-score
    stop_increasing = 0

    train_ml_losses = []
    total_batch = -1
    early_stop_flag = False
    replay_memory = ReplayMemory(opt.replay_buffer_capacity)

    for epoch in range(opt.start_epoch, opt.epochs):
        if early_stop_flag:
            break

        progbar = Progbar(logger=logging, title='Training', target=len(train_data_loader), batch_size=train_data_loader.batch_size,
                          total_examples=len(train_data_loader.dataset.examples))

        for batch_i, batch in enumerate(train_data_loader):
            model.train()
            total_batch += 1
            one2seq_batch, _ = batch
            report_loss = []

            # Training
            if opt.train_ml:
                loss_ml, decoder_log_probs, nll_loss, penalty, te_loss = train_ml(
                    one2seq_batch, model, optimizer_ml, criterion, replay_memory, opt)
                train_ml_losses.append(loss_ml)
                report_loss.append(('train_ml_loss', loss_ml))
                report_loss.append(('PPL', loss_ml))
                report_loss.append(('nll_loss', nll_loss))
                report_loss.append(('penalty', penalty))
                report_loss.append(('te_loss', te_loss))

                # Brief report
                if batch_i % opt.report_every == 0:
                    brief_report(epoch, batch_i, one2seq_batch,
                                 loss_ml, decoder_log_probs, opt)

            progbar.update(epoch, batch_i, report_loss)

            # Validate and save checkpoint
            if (opt.run_valid_every == -1 and batch_i == len(train_data_loader) - 1) or\
               (opt.run_valid_every > -1 and total_batch > 1 and total_batch % opt.run_valid_every == 0):
                logging.info('*' * 50)
                logging.info('Run validing and testing @Epoch=%d,#(Total batch)=%d' % (
                    epoch, total_batch))
                

                valid_loss = evaluate_nll_loss(model, valid_data_loader, criterion, opt ,title='Validating, epoch=%d, batch=%d, total_batch=%d' % (
                    epoch, batch_i, total_batch), epoch=epoch, save_path=opt.pred_path + '/epoch%d_batch%d_total_batch%d' % (epoch, batch_i, total_batch))
                test_loss = evaluate_nll_loss(model, test_data_loader, criterion, opt, title='Testing, epoch=%d, batch=%d, total_batch=%d' % (
                    epoch, batch_i, total_batch), epoch=epoch, save_path=opt.pred_path + '/epoch%d_batch%d_total_batch%d' % (epoch, batch_i, total_batch))

                '''
                determine if early stop training (whether f-score increased, before is if valid error decreased)
                '''
                is_best_loss = valid_loss > best_loss
                best_loss = min(valid_loss, best_loss)

                # only store the checkpoints that make better validation
                # performances
                # epoch >= opt.start_checkpoint_at and
                if total_batch > 1 and (total_batch % opt.save_model_every == 0 or is_best_loss):
                    # Save the checkpoint
                    logging.info('Saving checkpoint to: %s' % os.path.join(opt.model_path, '%s.epoch=%d.batch=%d.total_batch=%d.error=%f' % (
                        opt.exp, epoch, batch_i, total_batch, valid_loss) + '.model'))
                    torch.save(
                        model.state_dict(),
                        os.path.join(opt.model_path, '%s.epoch=%d.batch=%d.total_batch=%d' % (
                            opt.exp, epoch, batch_i, total_batch) + '.model')
                    )
                logging.info('*' * 50)


def load_data_vocab(opt, load_train=True):

    logging.info("Loading vocab from disk: %s" % (opt.vocab))
    word2id, id2word, vocab = torch.load(opt.vocab, 'wb')

    # one2one data loader
    logging.info("Loading train and validate data from '%s'" % opt.data)
    '''
    train_one2one  = torch.load(opt.data + '.train.one2one.pt', 'wb')
    valid_one2one  = torch.load(opt.data + '.valid.one2one.pt', 'wb')

    train_one2one_dataset = KeyphraseDataset(train_one2one, word2id=word2id)
    valid_one2one_dataset = KeyphraseDataset(valid_one2one, word2id=word2id)
    train_one2one_loader = DataLoader(dataset=train_one2one_dataset, collate_fn=train_one2one_dataset.collate_fn_one2one, num_workers=opt.batch_workers, batch_size=opt.batch_size, pin_memory=True, shuffle=True)
    valid_one2one_loader = DataLoader(dataset=valid_one2one_dataset, collate_fn=valid_one2one_dataset.collate_fn_one2one, num_workers=opt.batch_workers, batch_size=opt.batch_size, pin_memory=True, shuffle=False)
    '''

    logging.info('======================  Dataset  =========================')
    # one2many data loader
    if load_train:
        train_one2seq = torch.load(opt.data + '.train.one2many.pt', 'wb')
        train_one2seq_dataset = KeyphraseDataset(
            train_one2seq, word2id=word2id, id2word=id2word, type='one2seq', ordering=opt.keyphrase_ordering)
        train_one2seq_loader = KeyphraseDataLoader(dataset=train_one2seq_dataset, collate_fn=train_one2seq_dataset.collate_fn_one2seq,
                                                   num_workers=opt.batch_workers, max_batch_example=1024, max_batch_pair=opt.batch_size, pin_memory=True, shuffle=True)
        logging.info('#(train data size: #(one2many pair)=%d, #(one2one pair)=%d, #(batch)=%d, #(average examples/batch)=%.3f' % (len(train_one2seq_loader.dataset),
                                                                                                                                  train_one2seq_loader.one2one_number(), len(train_one2seq_loader), train_one2seq_loader.one2one_number() / len(train_one2seq_loader)))
    else:
        train_one2seq_loader = None

    valid_one2seq = torch.load(opt.data + '.valid.one2many.pt', 'wb')
    test_one2seq = torch.load(opt.data + '.test.one2many.pt', 'wb')

    # !important. As it takes too long to do beam search, thus reduce the size of validation and test datasets
    if opt.test_2k:
        valid_one2seq = valid_one2seq[:2000]
        test_one2seq = test_one2seq[:2000]

    valid_one2seq_dataset = KeyphraseDataset(
        valid_one2seq, word2id=word2id, id2word=id2word, type='one2seq', include_original=True, ordering=opt.keyphrase_ordering)
    test_one2seq_dataset = KeyphraseDataset(
        test_one2seq, word2id=word2id, id2word=id2word, type='one2seq', include_original=True, ordering=opt.keyphrase_ordering)

    """
    # temporary code, exporting test data for Theano model
    for e_id, e in enumerate(test_one2seq_dataset.examples):
        with open(os.path.join('data', 'new_kp20k_for_theano_model', 'text', '%d.txt' % e_id), 'w') as t_file:
            t_file.write(' '.join(e['src_str']))
        with open(os.path.join('data', 'new_kp20k_for_theano_model', 'keyphrase', '%d.txt' % e_id), 'w') as t_file:
            t_file.writelines([(' '.join(t))+'\n' for t in e['trg_str']])
    exit()
    """

    valid_one2seq_loader = KeyphraseDataLoader(dataset=valid_one2seq_dataset, collate_fn=valid_one2seq_dataset.collate_fn_one2seq, num_workers=opt.batch_workers,
                                               max_batch_example=opt.beam_search_batch_example, max_batch_pair=opt.beam_search_batch_size, pin_memory=True, shuffle=False)
    test_one2seq_loader = KeyphraseDataLoader(dataset=test_one2seq_dataset, collate_fn=test_one2seq_dataset.collate_fn_one2seq, num_workers=opt.batch_workers,
                                              max_batch_example=opt.beam_search_batch_example, max_batch_pair=opt.beam_search_batch_size, pin_memory=True, shuffle=False)

    opt.word2id = word2id
    opt.id2word = id2word
    opt.vocab = vocab

    logging.info('#(vocab)=%d' % len(vocab))
    logging.info('#(vocab used)=%d' % opt.vocab_size)

    return train_one2seq_loader, valid_one2seq_loader, test_one2seq_loader, word2id, id2word, vocab


def init_optimizer_criterion(model, opt):
    """
    mask the PAD <pad> when computing loss, before we used weight matrix, but not handy for copy-model, change to ignore_index
    :param model:
    :param opt:
    :return:
    """
    '''
    if not opt.copy_attention:
        weight_mask = torch.ones(opt.vocab_size).cuda() if torch.cuda.is_available() else torch.ones(opt.vocab_size)
    else:
        weight_mask = torch.ones(opt.vocab_size + opt.max_unk_words).cuda() if torch.cuda.is_available() else torch.ones(opt.vocab_size + opt.max_unk_words)
    weight_mask[opt.word2id[pykp.IO.PAD_WORD]] = 0
    criterion = torch.nn.NLLLoss(weight=weight_mask)

    optimizer = Adam(params=filter(lambda p: p.requires_grad, model.parameters()), lr=opt.learning_rate)
    # optimizer = torch.optim.Adadelta(model.parameters(), lr=0.1)
    # optimizer = torch.optim.RMSprop(model.parameters(), lr=0.1)
    '''
    criterion = torch.nn.NLLLoss(ignore_index=opt.word2id[pykp.io.PAD_WORD])

    if opt.train_ml:
        optimizer_ml = Adam(params=filter(
            lambda p: p.requires_grad, model.parameters()), lr=opt.learning_rate)
    else:
        optimizer_ml = None

    if torch.cuda.is_available():
        criterion = criterion.cuda()

    return optimizer_ml, None, criterion


def init_model(opt):
    logging.info(
        '======================  Model Parameters  =========================')

    if opt.cascading_model:
        model = Seq2SeqLSTMAttentionCascading(opt)
    else:
        if opt.copy_attention:
            logging.info('Train a Seq2Seq model with Copy Mechanism')
        else:
            logging.info('Train a normal Seq2Seq model')
        model = Seq2SeqLSTMAttention(opt)

    if opt.train_from:
        logging.info("loading previous checkpoint from %s" % opt.train_from)
        if torch.cuda.is_available():
            model.load_state_dict(torch.load(opt.train_from))
        else:
            model.load_state_dict(torch.load(
                opt.train_from, map_location={'cuda:0': 'cpu'}))

    if torch.cuda.is_available():
        model = model.cuda()

    utils.tally_parameters(model)

    return model


def process_opt(opt):
    if opt.seed > 0:
        torch.manual_seed(opt.seed)

    if torch.cuda.is_available() and not opt.gpuid:
        opt.gpuid = 0

    if hasattr(opt, 'train_ml') and opt.train_ml:
        opt.exp += '.ml'

    if hasattr(opt, 'copy_attention') and opt.copy_attention:
        opt.exp += '.copy'

    if hasattr(opt, 'bidirectional') and opt.bidirectional:
        opt.exp += '.bi-directional'
    else:
        opt.exp += '.uni-directional'

    # fill time into the name
    if opt.exp_path.find('%s') > 0:
        opt.exp_path = opt.exp_path % (opt.exp, opt.timemark)
        opt.pred_path = opt.pred_path % (opt.exp, opt.timemark)
        opt.model_path = opt.model_path % (opt.exp, opt.timemark)

    if not os.path.exists(opt.exp_path):
        os.makedirs(opt.exp_path)
    if not os.path.exists(opt.pred_path):
        os.makedirs(opt.pred_path)
    if not os.path.exists(opt.model_path):
        os.makedirs(opt.model_path)

    logging.info('EXP_PATH : ' + opt.exp_path)

    # dump the setting (opt) to disk in order to reuse easily
    json.dump(vars(opt), open(os.path.join(
        opt.model_path, opt.exp + '.initial.json'), 'w'))

    return opt


def main():
    # load settings for training
    parser = argparse.ArgumentParser(
        description='train_cas.py',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    config.preprocess_opts(parser)
    config.model_opts(parser)
    config.train_opts(parser)
    config.predict_opts(parser)
    opt = parser.parse_args()
    opt = process_opt(opt)
    opt.input_feeding = False
    opt.copy_input_feeding = False

    logging = config.init_logging(
        logger_name=None, log_file=opt.exp_path + '/output.log', stdout=True)

    logging.info('Parameters:')
    [logging.info('%s    :    %s' % (k, str(v)))
     for k, v in opt.__dict__.items()]

    try:
        train_data_loader, valid_data_loader, test_data_loader, word2id, id2word, vocab = load_data_vocab(
            opt)
        model = init_model(opt)
        optimizer_ml, optimizer_rl, criterion = init_optimizer_criterion(
            model, opt)
        train_model(model, optimizer_ml, optimizer_rl, criterion,
                    train_data_loader, valid_data_loader, test_data_loader, opt)
    except Exception as e:
        logging.exception("message")


if __name__ == '__main__':
    main()