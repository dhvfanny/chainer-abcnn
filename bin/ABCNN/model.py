# -*- coding: utf-8 -*-
import sys
import chainer
import chainer.functions as F
import chainer.links as L
import numpy as np
from chainer import cuda, Function, Variable, reporter
from chainer import Link, Chain
from .util import cos_sim, debug_print
from itertools import product

def match_score(xi, yi):
    tmp = xi - yi
    tmp = tmp * tmp
    return 1 / (1 + F.sqrt(F.sum(tmp, axis=2) + 0.00001))

class ABCNN(Chain):

    def __init__(self, n_vocab, embed_dim, input_channel, output_channel, x1s_len, x2s_len, model_type, single_attention_mat=False, train=True):
        self.train = train
        self.embed_dim = embed_dim
        self.single_attention_mat = single_attention_mat
        self.model_type = model_type
        self.output_channel = output_channel
        # self.epsilon = Variable(self.xp.array([0.00001], dtype=np.float32))

        # use same matrix for transforming attention matrix
        if single_attention_mat:
            self.x1s_len = self.x2s_len = max(x1s_len, x2s_len)
            super(ABCNN, self).__init__(
                # embed=L.EmbedID(n_vocab, embed_dim, initialW=np.random.uniform(-0.01, 0.01)),  # 100: word-embedding vector size
                embed=L.EmbedID(n_vocab, embed_dim),  # 100: word-embedding vector size
                conv1=L.Convolution2D(
                    input_channel, output_channel, (4, embed_dim), pad=(3,0)),
                l1=L.Linear(in_size=2+4, out_size=1),  # 4 are from lexical features of WikiQA Task
                W0=L.Linear(in_size=embed_dim, out_size=self.x1s_len)
            )
        else:
        # use different matrix for each side of the model (i.e. x1s and x2s)
            self.x1s_len = x1s_len
            self.x2s_len = x2s_len
            super(ABCNN, self).__init__(
                # embed=L.EmbedID(n_vocab, embed_dim, initialW=np.random.uniform(-0.01, 0.01)),  # 100: word-embedding vector size
                embed=L.EmbedID(n_vocab, embed_dim),  # 100: word-embedding vector size
                conv1=L.Convolution2D(
                    input_channel, output_channel, (4, embed_dim), pad=(3,0)),
                l1=L.Linear(in_size=2+4, out_size=1),  # 4 are from lexical features of WikiQA Task
                W0=L.Linear(in_size=embed_dim, out_size=x2s_len),
                W1=L.Linear(in_size=embed_dim, out_size=x1s_len)
            )

    def load_glove_embeddings(self, glove_path, vocab):
        assert self.embed != None
        print("loading GloVe vector...", end='', flush=True, file=sys.stderr)
        with open(glove_path, "r") as fi:
            for line in fi:
                line_list = line.strip().split(" ")
                word = line_list[0]
                if word in vocab:
                    vec = self.xp.array(line_list[1::], dtype=np.float32)
                    self.embed.W.data[vocab[word]] = vec
        print("done", flush=True, file=sys.stderr)

    def load_word2vec_embeddings(self, word2vec_path, vocab):
        assert self.embed != None
        print("loading word2vec vector...", end='', flush=True, file=sys.stderr)
        with open(word2vec_path, "r") as fi:
            for n, line in enumerate(fi):
                # 1st line contains stats
                if n == 0:
                    continue
                line_list = line.strip().split(" ", 1)
                word = line_list[0]
                if word in vocab:
                    vec = self.xp.array(line.strip().split(" ")[1::], dtype=np.float32)
                    self.embed.W.data[vocab[word]] = vec
        print("done", flush=True, file=sys.stderr)

    @profile
    def __call__(self, x1s, x2s, wordcnt, wgt_wordcnt, x1s_len, x2s_len):
        batchsize = x1s.shape[0]
        ex1s = self.get_embeddings(x1s)
        ex2s = self.get_embeddings(x2s)

        if self.model_type == 'ABCNN1' or self.model_type == 'ABCNN3':
            attention_mat = F.squeeze(self.build_attention_mat(ex1s, ex2s), axis=1)
            identity = self.xp.identity(self.embed_dim, dtype=self.xp.float32)
            if self.single_attention_mat:
                W0 = W1 = self.W0(identity)
            else:
                W0 = self.W0(identity)
                W1 = self.W1(identity)
            W0 = F.tile(F.expand_dims(W0, axis=0), reps=(batchsize,1,1))
            x1s_attention = F.reshape(F.batch_matmul(W0, attention_mat, transb=True), (batchsize, 1, self.x1s_len, self.embed_dim))
            x1s_conv1_input = F.concat([ex1s, x1s_attention], axis=1)

            W1 = F.tile(F.expand_dims(W1, axis=0), reps=(batchsize,1,1))  # seemingly faster than stack * batchsize
            x1s_attention = F.reshape(F.batch_matmul(W0, attention_mat, transb=True), (batchsize, 1, self.x1s_len, self.embed_dim))
            x2s_attention = F.reshape(F.batch_matmul(W1, attention_mat), (batchsize, 1, self.x2s_len, self.embed_dim))
            x2s_conv1_input = F.concat([ex2s, x2s_attention], axis=1)
        else:  # dealing with ABCNN2
            x1s_conv1_input = ex1s
            x2s_conv1_input = ex2s

        x1s_conv1_output = self.wide_convolution(x1s_conv1_input)
        x2s_conv1_output = self.wide_convolution(x2s_conv1_input)

        if self.model_type == 'ABCNN2' or self.model_type == 'ABCNN3': #ABCNN-2
            # build attention matrix from output of wide-convolution
            attention_mat = F.squeeze(self.build_attention_mat(x1s_conv1_output, x2s_conv1_output), axis=1)

            # col-wise sum for x1s
            col_wise_sums = F.sum(attention_mat, axis=2)
            shape = col_wise_sums.shape
            col_wise_sums = F.reshape(col_wise_sums, (batchsize, 1, shape[1], 1))
            attention_coefs = F.tile(col_wise_sums, reps=(1,1,1,self.output_channel))
            x1s_avg_pool_input = x1s_conv1_output * attention_coefs

            # row-wise sum for x2s
            row_wise_sums = F.sum(attention_mat, axis=1)
            shape = row_wise_sums.shape
            row_wise_sums = F.reshape(row_wise_sums, (batchsize, 1, shape[1], 1))
            attention_coefs = F.tile(row_wise_sums, reps=(1,1,1,self.output_channel))
            x2s_avg_pool_input = x2s_conv1_output * attention_coefs
        else: # not ABCNN-2
            x1s_avg_pool_input = x1s_conv1_output
            x2s_avg_pool_input = x2s_conv1_output

        x1s_avg = F.average_pooling_2d(x1s_avg_pool_input, ksize=(4, 1), stride=1, use_cudnn=False)
        x2s_avg = F.average_pooling_2d(x2s_avg_pool_input, ksize=(4, 1), stride=1, use_cudnn=False)

        # average pooling from the very top of the model (i.e. block[-1])
        x1s_all_pool = F.average_pooling_2d(x1s_avg, ksize=(x1s_avg.shape[2], 1))
        x2s_all_pool = F.average_pooling_2d(x2s_avg, ksize=(x2s_avg.shape[2], 1))
        avg_pool_sim_score = F.squeeze(cos_sim(x1s_all_pool, x2s_all_pool), axis=2)

        # average pooling from the embedding layer
        # essentially this is equivalent to adding the bag-of-words feature
        ex1s_all_pool = F.average_pooling_2d(ex1s, ksize=(ex1s.shape[2], 1))
        ex2s_all_pool = F.average_pooling_2d(ex2s, ksize=(ex2s.shape[2], 1))
        embed_sim_score = F.squeeze(cos_sim(ex1s_all_pool, ex2s_all_pool), axis=2)

        feature_vec = F.concat([avg_pool_sim_score, embed_sim_score, wordcnt, wgt_wordcnt, x1s_len, x2s_len], axis=1)
        fc_out = F.squeeze(self.l1(feature_vec), axis=1)
        if self.train:
            return fc_out
        else:
            sim_scores = [avg_pool_sim_score, embed_sim_score]
            return fc_out, sim_scores

    def get_embeddings(self, xs):
        exs = self.embed(xs)
        batchsize, height, width = exs.shape
        exs = F.reshape(exs, (batchsize, 1, height, width))
        exs.unchain_backward()  # don't move word vector
        return exs

    def build_attention_mat(self, x1s, x2s):
        x1s_len = x1s.shape[2]
        x2s_len = x2s.shape[2]
        batchsize = x1s.shape[0]

        x1s = F.squeeze(x1s, axis=1)
        x2s = F.squeeze(x2s, axis=1)
        x1s_x2s = F.batch_matmul(x1s, x2s, transb=True)
        x1s_squared = F.tile(F.expand_dims(F.sum(F.square(x1s), axis=2), axis=2), reps=(1, 1, x2s.shape[1]))
        x2s_squared = F.tile(F.expand_dims(F.sum(F.square(x2s), axis=2), axis=1), reps=(1, x1s.shape[1], 1))
        epsilon = Variable(self.xp.full((batchsize, x1s_len, x2s_len), 0.10, dtype=np.float32))
        denominator = 1.0 + F.sqrt(x1s_squared + (-2 * x1s_x2s) + x2s_squared + 0.00001)
        denominator = F.maximum(epsilon, denominator)

        return F.expand_dims(1.0 / denominator, axis=1)

    def wide_convolution(self, xs):
        xs_conv1 = F.tanh(self.conv1(xs))
        # (batchsize, depth, width, height)
        xs_conv1_swap = F.swapaxes(xs_conv1, 1, 3)  # (3, 50, 20, 1) --> (3, 1, 20, 50)
        return xs_conv1_swap
