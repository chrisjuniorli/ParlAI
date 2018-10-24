#!/usr/bin/env python3

# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.


"""
Generic Pytorch-based Generator agent. Implements quite a bit of boilerplate,
including Beam search.

Contains the following utilities:

* TorchGeneratorAgent class, which serves as a useful parent for generative torch
  agents.
* Beam class which provides some generic beam functionality for classes to use

TODO: Docs.
"""

import os
import math
import tempfile
from collections import defaultdict, Counter, namedtuple
from operator import attrgetter

import torch
import torch.nn as nn
import torch.nn.functional as F

from parlai.core.torch_agent import TorchAgent, Output
from parlai.core.utils import NEAR_INF, padded_tensor, round_sigfigs
from parlai.core.thread_utils import SharedTable


class TorchGeneratorAgent(TorchAgent):
    """Abstract Generator agent.

    TODO: write docs"""
    @classmethod
    def add_cmdline_args(cls, argparser):
        agent = argparser.add_argument_group('Torch Generator Agent')
        agent.add_argument('--beam-size', type=int, default=1,
                           help='Beam size, if 1 then greedy search')
        agent.add_argument('--beam-dot-log', type='bool', default=False,
                           help='Dump beam trees as png dot images into /tmp folder')
        agent.add_argument('--beam-min-n-best', type=int, default=3,
                           help='Minimum number of nbest candidates to achieve '
                                'during the beam search')
        agent.add_argument('--beam-min-length', type=int, default=3,
                           help='Minimum length of prediction to be generated by '
                                'the beam search')
        agent.add_argument('-idr', '--input-dropout', type=float, default=0.0,
                           help='Each token from the input will be masked with'
                                ' __unk__ token with this probability.')
        agent.add_argument('--beam-block-ngram', type=int, default=0,
                           help='Block all repeating ngrams up to history length n-1')

        super(TorchGeneratorAgent, cls).add_cmdline_args(argparser)
        return agent

    def __init__(self, opt, shared=None):
        init_model = None
        if not shared:  # only do this on first setup
            # first check load path in case we need to override paths
            if opt.get('init_model') and os.path.isfile(opt['init_model']):
                # check first for 'init_model' for loading model from file
                init_model = opt['init_model']
            if opt.get('model_file') and os.path.isfile(opt['model_file']):
                # next check for 'model_file', this would override init_model
                init_model = opt['model_file']

            if init_model is not None:
                # if we are loading a model, should load its dict too
                if (os.path.isfile(init_model + '.dict') or
                        opt['dict_file'] is None):
                    opt['dict_file'] = init_model + '.dict'
        super().__init__(opt, shared)

        self.beam_dot_log = opt.get('beam_dot_log', False)
        self.beam_size = opt.get('beam_size', 1)
        self.beam_min_n_best = opt.get('beam_min_n_best', 3)
        self.beam_min_length = opt.get('beam_min_length', 3)
        self.beam_block_ngram = opt.get('beam_block_ngram', 0)

        if shared:
            # set up shared properties
            self.model = shared['model']
            self.metrics = shared['metrics']
            states = shared.get('states', {})
        else:
            self.metrics = {
                'loss': 0.0,
                'num_tokens': 0,
                'correct_tokens': 0,
                'total_skipped_batches': 0
            }
            # this is not a shared instance of this class, so do full init
            if self.beam_dot_log:
                self.beam_dot_dir = tempfile.mkdtemp(
                    prefix='{}-beamdot-beamsize-{}-'.format(
                        os.path.basename(
                            opt.get('model_file')),
                        self.beam_size))
                print(
                    '[ Saving dot beam logs in {} ]'.format(
                        self.beam_dot_dir))
            if init_model is not None:
                # load model parameters if available
                print('[ Loading existing model params from {} ]'
                      ''.format(init_model))
                states = self.load(init_model)
            else:
                states = {}

            self.build_criterion()
            self.build_model(states=states)

            if 'train' in opt.get('datatype', ''):
                self.init_optim(
                    [p for p in self.model.parameters() if p.requires_grad],
                    optim_states=states.get('optimizer'),
                    saved_optim_type=states.get('optimizer_type'))

        self.reset()

    def _v2t(self, vec):
        """Convert token indices to string of tokens."""
        new_vec = []
        if hasattr(vec, 'cpu'):
            vec = vec.cpu()
        for i in vec:
            if i == self.END_IDX:
                break
            elif i != self.START_IDX:
                new_vec.append(i)
        return self.dict.vec2txt(new_vec)

    def build_criterion(self):
        self.criterion = nn.CrossEntropyLoss(
            ignore_index=self.NULL_IDX, reduction='sum'
        )
        if self.use_cuda:
            self.criterion.cuda()

    def _init_cuda_buffer(self, model, criterion, batchsize, maxlen):
        """Pre-initialize CUDA buffer by doing fake forward pass."""
        if self.use_cuda and not hasattr(self, 'buffer_initialized'):
            try:
                print('preinitializing pytorch cuda buffer')
                dummy = torch.ones(batchsize, maxlen).long().cuda()
                out = model(dummy, dummy)
                sc = out[0]  # scores
                loss = criterion(sc.view(-1, sc.size(-1)), dummy.view(-1))
                loss.backward()
                self.buffer_initialized = True
            except RuntimeError as e:
                if 'out of memory' in str(e):
                    m = ('CUDA OOM: Lower batch size (-bs) from {} or lower '
                         ' max sequence length (-tr) from {}'
                         ''.format(batchsize, maxlen))
                    raise RuntimeError(m)
                else:
                    raise e

    def zero_grad(self):
        """Zero out optimizer."""
        self.optimizer.zero_grad()

    def update_params(self):
        """Do one optimization step."""
        if self.clip > 0:
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.clip)
        self.optimizer.step()

    def reset_metrics(self):
        """Reset metrics for reporting loss and perplexity."""
        super().reset_metrics()
        self.metrics['loss'] = 0.0
        self.metrics['num_tokens'] = 0
        self.metrics['correct_tokens'] = 0

    def share(self):
        """Share internal states between parent and child instances."""
        shared = super().share()
        shared['model'] = self.model
        if self.opt.get('numthreads', 1) > 1:
            # we're doing hogwild so share the model too
            if isinstance(self.metrics, dict):
                # move metrics and model to shared memory
                self.metrics = SharedTable(self.metrics)
                self.model.share_memory()
            shared['states'] = {  # don't share optimizer states
                'optimizer_type': self.opt['optimizer'],
            }
        shared['metrics'] = self.metrics  # do after numthreads check
        if self.beam_dot_log is True:
            shared['beam_dot_dir'] = self.beam_dot_dir
        return shared

    def report(self):
        """Report loss and perplexity from model's perspective.

        Note that this includes predicting __END__ and __UNK__ tokens and may
        differ from a truly independent measurement.
        """
        m = {}
        num_tok = self.metrics['num_tokens']
        if num_tok > 0:
            if self.metrics['correct_tokens'] > 0:
                m['token_acc'] = self.metrics['correct_tokens'] / num_tok
            m['loss'] = self.metrics['loss'] / num_tok
            try:
                m['ppl'] = math.exp(m['loss'])
            except OverflowError:
                m['ppl'] = float('inf')
        if self.metrics['total_skipped_batches'] > 0:
            m['total_skipped_batches'] = self.metrics['total_skipped_batches']
        for k, v in m.items():
            # clean up: rounds to sigfigs and converts tensors to floats
            m[k] = round_sigfigs(v, 4)
        return m

    def train_step(self, batch):
        """Train on a single batch of examples."""
        batchsize = batch.text_vec.size(0)
        # helps with memory usage
        self._init_cuda_buffer(self.model, self.criterion, batchsize,
                               self.truncate or 180)
        self.model.train()
        self.zero_grad()

        try:
            out = self.model(batch.text_vec, batch.label_vec)

            # generated response
            scores = out[0]
            _, preds = scores.max(2)

            score_view = scores.view(-1, scores.size(-1))
            loss = self.criterion(score_view, batch.label_vec.view(-1))
            # save loss to metrics
            notnull = batch.label_vec.ne(self.NULL_IDX)
            target_tokens = notnull.long().sum().item()
            correct = ((batch.label_vec == preds) * notnull).sum().item()
            self.metrics['correct_tokens'] += correct
            self.metrics['loss'] += loss.item()
            self.metrics['num_tokens'] += target_tokens
            loss /= target_tokens  # average loss per token
            loss.backward()
            self.update_params()
        except RuntimeError as e:
            # catch out of memory exceptions during fwd/bck (skip batch)
            if 'out of memory' in str(e):
                print('| WARNING: ran out of memory, skipping batch. '
                      'if this happens frequently, decrease batchsize or '
                      'truncate the inputs to the model.')
                self.metrics['total_skipped_batches'] += 1
            else:
                raise e

    def _build_cands(self, batch):
        if not batch.candidates:
            return None, None
        cand_inds = [i for i in range(len(batch.candidates))
                     if batch.candidates[i]]
        cands = [batch.candidate_vecs[i] for i in cand_inds]
        for i, c in enumerate(cands):
            cands[i] = padded_tensor(c, use_cuda=self.use_cuda)[0]
        return cands, cand_inds

    def _pick_cands(self, cand_preds, cand_inds, cands):
        cand_replies = [None] * len(cands)
        for idx, order in enumerate(cand_preds):
            batch_idx = cand_inds[idx]
            cand_replies[batch_idx] = [cands[batch_idx][i] for i in order]
        return cand_replies

    def eval_step(self, batch):
        """Evaluate a single batch of examples."""
        if batch.text_vec is None:
            return
        self.model.eval()
        cand_scores = None
        if self.beam_size == 1:
            out, cand_params = self.greedy_search(batch)
            scores, cand_scores = out[0], out[1]
            _, preds = scores.max(2)
        elif self.beam_size > 1:
            out = self.beam_search(
                self.model,
                batch,
                self.beam_size,
                start=self.START_IDX,
                end=self.END_IDX,
                pad=self.NULL_IDX,
                min_length=self.beam_min_length,
                min_n_best=self.beam_min_n_best,
                block_ngram=self.beam_block_ngram)
            beam_preds_scores, _, beams = out
            preds, scores = [p[0] for p in beam_preds_scores], [
                p[1] for p in beam_preds_scores]
            if self.beam_dot_log is True:
                for i, b in enumerate(beams):
                    dot_graph = b.get_beam_dot(dictionary=self.dict, n_best=3)
                    image_name = self._v2t(batch.text_vec[i, -20:]).replace(
                        ' ',
                        '-').replace('__null__', '')
                    dot_graph.write_png(os.path.join(
                        self.beam_dot_dir, "{}.png".format(image_name)))

        if batch.label_vec is not None:
            # calculate loss on targets with teacher forcing
            out = self.model(batch.text_vec, batch.label_vec)
            f_scores = out[0]  # forced scores
            _, f_preds = f_scores.max(2)  # forced preds
            score_view = f_scores.view(-1, f_scores.size(-1))
            loss = self.criterion(score_view, batch.label_vec.view(-1))
            # save loss to metrics
            notnull = batch.label_vec.ne(self.NULL_IDX)
            target_tokens = notnull.long().sum().item()
            correct = ((batch.label_vec == f_preds) * notnull).sum().item()
            self.metrics['correct_tokens'] += correct
            self.metrics['loss'] += loss.item()
            self.metrics['num_tokens'] += target_tokens

        cand_choices = None
        if cand_scores is not None:
            cand_preds = cand_scores.sort(1, descending=True)[1]
            # now select the text of the cands based on their scores
            cand_choices = self._pick_cands(cand_preds, cand_params[1],
                                            batch.candidates)

        text = [self._v2t(p) for p in preds]
        return Output(text, cand_choices)

    def greedy_search(self, batch):
        cand_params = self._build_cands(batch)
        out = self.model(batch.text_vec, ys=None, cand_params=cand_params)
        return out, cand_params

    def beam_search(self, model, batch, beam_size, start=1, end=2,
                    pad=0, min_length=3, min_n_best=5, max_ts=40, block_ngram=0):
        """ Beam search given the model and Batch

        This function uses model with the following reqs:

        - model.encoder takes input returns tuple (enc_out, enc_hidden, attn_mask)
        - model.decoder takes decoder params and returns decoder outputs after attn
        - model.output takes decoder outputs and returns distr over dictionary

        Function arguments:
        model : nn.Module, here defined in modules.py
        batch : Batch structure with input and labels
        beam_size : Size of each beam during the search
        start : start of sequence token
        end : end of sequence token
        pad : padding token
        min_length : minimum length of the decoded sequence
        min_n_best : minimum number of completed hypothesis generated from each beam
        max_ts: the maximum length of the decoded sequence

        Return:
        beam_preds_scores : list of tuples (prediction, score) for each sample in Batch
        n_best_preds_scores : list of n_best list of tuples (prediction, score) for
                              each sample from Batch
        beams : list of Beam instances defined in Beam class, can be used for any
                following postprocessing, e.g. dot logging.
        """
        encoder_states = model.encoder(batch.text_vec)
        enc_out = encoder_states[0]
        enc_hidden = encoder_states[1]
        attn_mask = encoder_states[2]
        current_device = encoder_states[0][0].device

        batch_size = len(batch.text_lengths)
        beams = [Beam(beam_size, min_length=min_length, padding_token=pad,
                      bos_token=start, eos_token=end, min_n_best=min_n_best,
                      cuda=current_device,
                      block_ngram=block_ngram) for i in range(batch_size)]
        decoder_input = torch.Tensor([start]).detach().expand(
            batch_size, 1).long().to(current_device)
        # repeat encoder_outputs, hiddens, attn_mask
        decoder_input = decoder_input.repeat(
            1, beam_size).view(beam_size * batch_size, -1)
        enc_out = enc_out.unsqueeze(1).repeat(1, beam_size, 1, 1).view(
            batch_size * beam_size, -1, enc_out.size(-1))
        attn_mask = encoder_states[2].repeat(
            1, beam_size).view(attn_mask.size(0) * beam_size, -1)
        repeated_hiddens = []
        if isinstance(enc_hidden, tuple):  # LSTM
            for i in range(len(enc_hidden)):
                repeated_hiddens.append(
                    enc_hidden[i].unsqueeze(2).repeat(1, 1, beam_size, 1))
            num_layers = enc_hidden[0].size(0)
            hidden_size = enc_hidden[0].size(-1)
            enc_hidden = tuple([repeated_hiddens[i].view(
                num_layers, batch_size *
                beam_size, hidden_size) for i in range(len(repeated_hiddens))])
        else:  # GRU
            num_layers = enc_hidden.size(0)
            hidden_size = enc_hidden.size(-1)
            enc_hidden = enc_hidden.unsqueeze(2).repeat(1, 1, beam_size, 1).view(
                num_layers, batch_size * beam_size, hidden_size)

        hidden = enc_hidden
        for ts in range(max_ts):
            if all((b.done() for b in beams)):
                break
            output, hidden = model.decoder(
                decoder_input, hidden, (enc_out, attn_mask))
            score = model.output(output)
            # score contains softmax scores for batch_size * beam_size samples
            score = score.view(batch_size, beam_size, -1)
            score = F.log_softmax(score, dim=-1)
            for i, b in enumerate(beams):
                b.advance(score[i])
            decoder_input = torch.cat(
                [b.get_output_from_current_step() for b in beams]).unsqueeze(-1)
            permute_hidden_idx = torch.cat(
                [beam_size * i +
                    b.get_backtrack_from_current_step() for i, b in enumerate(beams)])
            # permute decoder hiddens with respect to chosen hypothesis now
            if isinstance(hidden, tuple):  # LSTM
                for i in range(len(hidden)):
                    hidden[i].data.copy_(hidden[i].data.index_select(
                        dim=1, index=permute_hidden_idx))
            else:  # GRU
                hidden.data.copy_(hidden.data.index_select(
                    dim=1, index=permute_hidden_idx))
        for b in beams:
            b.check_finished()

        beam_preds_scores = [list(b.get_top_hyp()) for b in beams]
        for pair in beam_preds_scores:
            pair[0] = Beam.get_pretty_hypothesis(pair[0])

        n_best_beams = [b.get_rescored_finished(
            n_best=min_n_best) for b in beams]
        n_best_beam_preds_scores = []
        for i, beamhyp in enumerate(n_best_beams):
            this_beam = []
            for hyp in beamhyp:
                pred = beams[i].get_pretty_hypothesis(
                    beams[i].get_hyp_from_finished(hyp))
                score = hyp.score
                this_beam.append((pred, score))
            n_best_beam_preds_scores.append(this_beam)

        return beam_preds_scores, n_best_beam_preds_scores, beams


class mydefaultdict(defaultdict):
    """Get function also uses default_factory for this defaultdict.

    This makes dict.get() behave like dict[] if a default is not provided.
    """

    def get(self, key, default=None):
        """Return value at key or default if key is not in dict.

        If a default is not provided, return the default factory value.
        """
        # override default from "get" (like "__getitem__" already is)
        return super().get(key, default or self.default_factory())


class PerplexityEvaluatorAgent(TorchGeneratorAgent):
    """Subclass for doing standardized perplexity evaluation.

    This is designed to be used in conjunction with the PerplexityWorld at
    parlai/scripts/eval_ppl.py. It uses the `next_word_probability` function
    to calculate the probability of tokens one token at a time.
    """

    def __init__(self, opt, shared=None):
        """Initialize evaluator."""
        super().__init__(opt, shared)
        self.prev_enc = None
        self.last_xs = None

    def next_word_probability(self, partial_out):
        """Return probability distribution over next words.

        This probability is based on both nn input and partial true output.
        This is used to calculate the per-word perplexity.

        Arguments:
        observation -- input observation dict
        partial_out -- list of previous "true" words

        Returns a dict, where each key is a word and each value is a
        probability score for that word.
        Unset keys will use a probability of 1e-7.

        e.g.
        {'text': 'Run test program.'}, ['hello'] => {'world': 1.0}
        """
        obs = self.observation
        xs = obs['text_vec'].unsqueeze(0)
        ys = self._vectorize_text(
            ' '.join(partial_out), False, True, self.truncate
        ).unsqueeze(0)
        if self.prev_enc is not None and self.last_xs is not None and (
                xs.shape[1] != self.last_xs.shape[1] or
                (xs == self.last_xs).sum().item() != xs.shape[1]):
            # reset prev_enc, this is a new input
            self.prev_enc = None
        self.last_xs = xs

        self.model.eval()
        out = self.model(
            xs,
            ys=(ys if len(partial_out) > 0 else None),
            prev_enc=self.prev_enc,
            maxlen=1)
        scores, self.prev_enc = out[0], out[2]
        # scores is bsz x seqlen x num_words, so select probs of current index
        probs = F.softmax(scores.select(1, -1), dim=1).squeeze()
        dist = mydefaultdict(lambda: 1e-7)  # default probability for any token
        for i in range(len(probs)):
            dist[self.dict[i]] = probs[i].item()
        return dist


class Beam(object):
    """Generic beam class. It keeps information about beam_size hypothesis."""

    def __init__(self, beam_size, min_length=3, padding_token=0, bos_token=1,
                 eos_token=2, min_n_best=3, cuda='cpu', block_ngram=0):
        """Instantiate Beam object.

        :param beam_size: number of hypothesis in the beam
        :param min_length: minimum length of the predicted sequence
        :param padding_token: Set to 0 as usual in ParlAI
        :param bos_token: Set to 1 as usual in ParlAI
        :param eos_token: Set to 2 as usual in ParlAI
        :param min_n_best: Beam will not be done unless this amount of finished
                           hypothesis (with EOS) is done
        :param cuda: What device to use for computations
        """
        self.beam_size = beam_size
        self.min_length = min_length
        self.eos = eos_token
        self.bos = bos_token
        self.pad = padding_token
        self.device = cuda
        # recent score for each hypo in the beam
        self.scores = torch.Tensor(self.beam_size).float().zero_().to(
            self.device)
        # self.scores values per each time step
        self.all_scores = [torch.Tensor([0.0] * beam_size).to(self.device)]
        # backtracking id to hypothesis at previous time step
        self.bookkeep = []
        # output tokens at each time step
        self.outputs = [torch.Tensor(self.beam_size).long()
                        .fill_(self.bos).to(self.device)]
        # keeps tuples (score, time_step, hyp_id)
        self.finished = []
        self.HypothesisTail = namedtuple(
            'HypothesisTail', ['timestep', 'hypid', 'score', 'tokenid'])
        self.eos_top = False
        self.eos_top_ts = None
        self.n_best_counter = 0
        self.min_n_best = min_n_best
        self.block_ngram = block_ngram

    @staticmethod
    def find_ngrams(input_list, n):
        """Get list of ngrams with context length n-1"""
        return list(zip(*[input_list[i:] for i in range(n)]))

    def get_output_from_current_step(self):
        """Get the outputput at the current step."""
        return self.outputs[-1]

    def get_backtrack_from_current_step(self):
        """Get the backtrack at the current step."""
        return self.bookkeep[-1]

    def advance(self, softmax_probs):
        """Advance the beam one step."""
        voc_size = softmax_probs.size(-1)
        current_length = len(self.all_scores) - 1
        if current_length < self.min_length:
            # penalize all eos probs to make it decode longer
            for hyp_id in range(softmax_probs.size(0)):
                softmax_probs[hyp_id][self.eos] = -NEAR_INF
        if len(self.bookkeep) == 0:
            # the first step we take only the first hypo into account since all
            # hypos are the same initially
            beam_scores = softmax_probs[0]
        else:
            # we need to sum up hypo scores and curr softmax scores before topk
            # [beam_size, voc_size]
            beam_scores = (softmax_probs +
                           self.scores.unsqueeze(1).expand_as(softmax_probs))
            for i in range(self.outputs[-1].size(0)):
                current_hypo = [ii.tokenid.item() for ii in
                                self.get_partial_hyp_from_tail(
                                len(self.outputs) - 1, i)][::-1][1:]
                if self.block_ngram > 0:
                    current_ngrams = []
                    for ng in range(self.block_ngram):
                        ngrams = Beam.find_ngrams(current_hypo, ng)
                        if len(ngrams) > 0:
                            current_ngrams.extend(ngrams)
                    counted_ngrams = Counter(current_ngrams)
                    if any(v > 1 for k, v in counted_ngrams.items()):
                        # block this hypothesis hard
                        beam_scores[i] = -NEAR_INF

                #  if previous output hypo token had eos
                # we penalize those word probs to never be chosen
                if self.outputs[-1][i] == self.eos:
                    # beam_scores[i] is voc_size array for i-th hypo
                    beam_scores[i] = -NEAR_INF

        flatten_beam_scores = beam_scores.view(-1)  # [beam_size * voc_size]
        with torch.no_grad():
            best_scores, best_idxs = torch.topk(
                flatten_beam_scores, self.beam_size, dim=-1)

        self.scores = best_scores
        self.all_scores.append(self.scores)
        # get the backtracking hypothesis id as a multiple of full voc_sizes
        hyp_ids = best_idxs / voc_size
        # get the actual word id from residual of the same division
        tok_ids = best_idxs % voc_size

        self.outputs.append(tok_ids)
        self.bookkeep.append(hyp_ids)

        #  check new hypos for eos label, if we have some, add to finished
        for hypid in range(self.beam_size):
            if self.outputs[-1][hypid] == self.eos:
                #  this is finished hypo, adding to finished
                eostail = self.HypothesisTail(timestep=len(self.outputs) - 1,
                                              hypid=hypid,
                                              score=self.scores[hypid],
                                              tokenid=self.eos)
                self.finished.append(eostail)
                self.n_best_counter += 1

        if self.outputs[-1][0] == self.eos:
            self.eos_top = True
            if self.eos_top_ts is None:
                self.eos_top_ts = len(self.outputs) - 1

    def done(self):
        """Return whether beam search is complete."""
        return self.eos_top and self.n_best_counter >= self.min_n_best

    def get_top_hyp(self):
        """Get single best hypothesis.

        :return: hypothesis sequence and the final score
        """
        top_hypothesis_tail = self.get_rescored_finished(n_best=1)[0]
        return (self.get_hyp_from_finished(top_hypothesis_tail),
                top_hypothesis_tail.score)

    def get_hyp_from_finished(self, hypothesis_tail):
        """Extract hypothesis ending with EOS at timestep with hyp_id.

        :param timestep: timestep with range up to len(self.outputs)-1
        :param hyp_id: id with range up to beam_size-1
        :return: hypothesis sequence
        """
        assert (self.outputs[hypothesis_tail.timestep]
                [hypothesis_tail.hypid] == self.eos)
        assert hypothesis_tail.tokenid == self.eos
        hyp_idx = []
        endback = hypothesis_tail.hypid
        for i in range(hypothesis_tail.timestep, -1, -1):
            hyp_idx.append(self.HypothesisTail(
                timestep=i, hypid=endback, score=self.all_scores[i][endback],
                tokenid=self.outputs[i][endback]))
            endback = self.bookkeep[i - 1][endback]

        return hyp_idx

    @staticmethod
    def get_pretty_hypothesis(list_of_hypotails):
        """Return prettier version of the hypotheses."""
        hypothesis = []
        for i in list_of_hypotails:
            hypothesis.append(i.tokenid)

        hypothesis = torch.stack(list(reversed(hypothesis)))

        return hypothesis

    def get_partial_hyp_from_tail(self, ts, hypid):
        hypothesis_tail = self.HypothesisTail(
            timestep=ts,
            hypid=torch.Tensor([hypid]).long(),
            score=self.all_scores[ts][hypid],
            tokenid=self.outputs[ts][hypid])
        hyp_idx = []
        endback = hypothesis_tail.hypid
        for i in range(hypothesis_tail.timestep, -1, -1):
            hyp_idx.append(self.HypothesisTail(
                timestep=i,
                hypid=endback,
                score=self.all_scores[i][endback],
                tokenid=self.outputs[i][endback]))
            endback = self.bookkeep[i - 1][endback]

        return hyp_idx

    def get_rescored_finished(self, n_best=None):
        """Return finished hypotheses in rescored order.

        :param n_best: how many n best hypothesis to return
        :return: list with hypothesis
        """
        rescored_finished = []
        for finished_item in self.finished:
            current_length = finished_item.timestep + 1
            # these weights are from Google NMT paper
            length_penalty = math.pow((1 + current_length) / 6, 0.65)
            rescored_finished.append(self.HypothesisTail(
                timestep=finished_item.timestep, hypid=finished_item.hypid,
                score=finished_item.score / length_penalty,
                tokenid=finished_item.tokenid))

        srted = sorted(rescored_finished, key=attrgetter('score'),
                       reverse=True)

        if n_best is not None:
            srted = srted[:n_best]

        return srted

    def check_finished(self):
        """Check if self.finished is empty and add hyptail in that case.

        This will be suboptimal hypothesis since the model did not get any EOS

        :returns: None
        """
        if len(self.finished) == 0:
            # we change output because we want outputs to have eos
            # to pass assert in L102, it is ok since empty self.finished
            # means junk prediction anyway
            self.outputs[-1][0] = self.eos
            hyptail = self.HypothesisTail(timestep=len(self.outputs) - 1,
                                          hypid=0,
                                          score=self.all_scores[-1][0],
                                          tokenid=self.outputs[-1][0])

            self.finished.append(hyptail)

    def get_beam_dot(self, dictionary=None, n_best=None):
        """Create pydot graph representation of the beam.

        :param outputs: self.outputs from the beam
        :param dictionary: tok 2 word dict to save words in the tree nodes
        :returns: pydot graph
        """
        try:
            import pydot
        except ImportError:
            print("Please install pydot package to dump beam visualization")

        graph = pydot.Dot(graph_type='digraph')
        outputs = [i.tolist() for i in self.outputs]
        bookkeep = [i.tolist() for i in self.bookkeep]
        all_scores = [i.tolist() for i in self.all_scores]
        if n_best is None:
            n_best = int(self.beam_size / 2)

        # get top nbest hyp
        top_hyp_idx_n_best = []
        n_best_colors = ['aquamarine', 'chocolate1', 'deepskyblue',
                         'green2', 'tan']
        sorted_finished = self.get_rescored_finished(n_best=n_best)
        for hyptail in sorted_finished:
            # do not include EOS since it has rescored score not from original
            # self.all_scores, we color EOS with black
            top_hyp_idx_n_best.append(self.get_hyp_from_finished(
                hyptail))

        # create nodes
        for tstep, lis in enumerate(outputs):
            for hypid, token in enumerate(lis):
                if tstep == 0:
                    hypid = 0  # collapse all __NULL__ nodes
                node_tail = self.HypothesisTail(timestep=tstep, hypid=hypid,
                                                score=all_scores[tstep][hypid],
                                                tokenid=token)
                color = 'white'
                rank = None
                for i, hypseq in enumerate(top_hyp_idx_n_best):
                    if node_tail in hypseq:
                        if n_best <= 5:  # color nodes only if <=5
                            color = n_best_colors[i]
                        rank = i
                        break
                label = (
                    "<{}".format(dictionary.vec2txt([token])
                                 if dictionary is not None else token) +
                    " : " +
                    "{:.{prec}f}>".format(all_scores[tstep][hypid], prec=3))

                graph.add_node(pydot.Node(
                    node_tail.__repr__(), label=label, fillcolor=color,
                    style='filled',
                    xlabel='{}'.format(rank) if rank is not None else ''))

        # create edges
        for revtstep, lis in reversed(list(enumerate(bookkeep))):
            for i, prev_id in enumerate(lis):
                from_node = graph.get_node(
                    '"{}"'.format(self.HypothesisTail(
                        timestep=revtstep, hypid=prev_id,
                        score=all_scores[revtstep][prev_id],
                        tokenid=outputs[revtstep][prev_id]).__repr__()))[0]
                to_node = graph.get_node(
                    '"{}"'.format(self.HypothesisTail(
                        timestep=revtstep + 1, hypid=i,
                        score=all_scores[revtstep + 1][i],
                        tokenid=outputs[revtstep + 1][i]).__repr__()))[0]
                newedge = pydot.Edge(from_node.get_name(), to_node.get_name())
                graph.add_edge(newedge)

        return graph
