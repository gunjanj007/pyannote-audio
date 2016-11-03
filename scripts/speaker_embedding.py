#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2016 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr

"""
Speaker embedding

Usage:
  speaker_embedding train [--subset=<subset> --duration=<duration>] <experiment_dir> <database.task.protocol> <wav_template>
  speaker_embedding tune [--subset=<subset> --false-alarm=<beta>] <train_dir> <database.task.protocol> <wav_template>
  speaker_embedding test [--subset=<subset> --false-alarm=<beta>] <tune_dir> <database.task.protocol> <wav_template>
  speaker_embedding apply [--subset=<subset> --step=<step>] <tune_dir> <database.task.protocol> <wav_template>
  speaker_embedding -h | --help
  speaker_embedding --version

Options:
  <experiment_dir>           Set experiment root directory. This script expects
                             a configuration file called "config.yml" to live
                             in this directory. See "Configuration file"
                             section below for more details.
  <database.task.protocol>   Set evaluation protocol (e.g. "Etape.SpeakerDiarization.TV")
  <wav_template>             Set path to actual wave files. This path is
                             expected to contain a {uri} placeholder that will
                             be replaced automatically by the actual unique
                             resource identifier (e.g. '/Etape/{uri}.wav').
  --subset=<subset>          Set subset (train|developement|test).
                             In "train" mode, default subset is "train".
                             In "tune" mode, default subset is "development".
                             In "apply" mode, default subset is "test".
  --false-alarm=<beta>       Set importance of false alarm with respect to
                             false rejection [default: 1.0]
  --step=<step>              Set step (in seconds) for embedding extraction.
                             [default: 0.1]
  -h --help                  Show this screen.
  --version                  Show version.

Configuration file:
    The configuration of each experiment is described in a file called
    <experiment_dir>/config.yml, that describes the architecture of the neural
    network used for sequence embedding, the feature extraction process
    (e.g. MFCCs) and the training strategy.

    ................... <experiment_dir>/config.yml ...................
    feature_extraction:
       name: YaafeMFCC
       params:
          e: False                   # this experiments relies
          De: True                   # on 11 MFCC coefficients
          DDe: True                  # with 1st and 2nd derivatives
          D: True                    # without energy, but with
          DD: True                   # energy derivatives

    architecture:
       name: TristouNet
       params:                        # this experiments relies
          lstm: [16]                  # on one LSTM layer (16 outputs)
          bidirectional: True         # which is bidirectional
          pooling: average            # and whose output are averaged over the sequence,
          dense: [16]                 # and one internal dense layer
          space: sphere               # embedding live on the unit hypersphere
          output_dim: 16              # of dimension 16
    ...................................................................

"train" mode:
    First, one should train the raw sequence embedding neural network using
    "train" mode. This will create the following directory that contains
    the pre-trained neural network weights after each epoch:

        <experiment_dir>/train/<database.task.protocol>.<subset>/<duration>

    This means that the network was trained on the <subset> subset of the
    <database.task.protocol> protocol. By default, <subset> is "train".
    This directory is called <train_dir> in the subsequent "tune" mode.

"tune" mode:
    Then, one should tune the hyper-parameters using "tune" mode.
    This will create the following directory that contains a file called
    "tune.yml" describing the best hyper-parameters to use:

        <train_dir>/tune/<database.task.protocol>.<subset>

    This means that hyper-parameters were tuned on the <subset> subset of the
    <database.task.protocol> protocol. By default, <subset> is "development".
    This directory is called <tune_dir> in the subsequence "apply" mode.

"test" mode:
    ...

"apply" mode
    Finally, one can apply the embedding using "apply" mode.
    This will create the following files that contains the hard and soft
    outputs of speech activity detection.

        <tune_dir>/apply/<database.task.protocol>.<subset>/{uri}.pkl

    This means that file whose unique resource identifier is {uri} has been
    processed.

"""

import yaml
import pickle
import os.path
import functools
import numpy as np

from docopt import docopt

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import pyannote.core

from pyannote.database import get_database
from pyannote.audio.optimizers import SSMORMS3

from pyannote.audio.embedding.base import SequenceEmbedding
from pyannote.audio.embedding.triplet_loss.glue import TripletLoss
from pyannote.audio.embedding.triplet_loss.generator import TripletBatchGenerator

from pyannote.audio.generators.labels import \
    LabeledFixedDurationSequencesBatchGenerator
from scipy.spatial.distance import pdist, squareform

from pyannote.audio.embedding.extraction import Extraction

import skopt
import skopt.utils
import skopt.space
import skopt.plots
import sklearn.metrics
from pyannote.metrics import f_measure


def train(protocol, duration, experiment_dir, train_dir, subset='train'):

    # -- TRAINING --
    batch_size = 1024
    nb_epoch = 1000
    optimizer = SSMORMS3()
    per_label = 40
    batch_size = 8192

    # load configuration file
    config_yml = experiment_dir + '/config.yml'
    with open(config_yml, 'r') as fp:
        config = yaml.load(fp)

    # -- FEATURE EXTRACTION --
    feature_extraction_name = config['feature_extraction']['name']
    features = __import__('pyannote.audio.features.yaafe',
                          fromlist=[feature_extraction_name])
    FeatureExtraction = getattr(features, feature_extraction_name)
    feature_extraction = FeatureExtraction(
        **config['feature_extraction'].get('params', {}))

    # -- ARCHITECTURE --
    architecture_name = config['architecture']['name']
    models = __import__('pyannote.audio.embedding.models',
                        fromlist=[architecture_name])
    Architecture = getattr(models, architecture_name)
    architecture = Architecture(
        **config['architecture'].get('params', {}))

    # -- LOSS --
    margin = 0.2
    glue = TripletLoss(margin=margin)

    # -- SEQUENCE GENERATOR --
    generator = TripletBatchGenerator(
        feature_extraction, protocol.train(), margin=margin,
        duration=duration, per_label=per_label, batch_size=batch_size)

    # input shape (n_frames, n_features)
    input_shape = generator.get_shape()

    # estimate number of triplets per epoch
    # (rounded to closest batch_size multiple)
    n_labels = len(protocol.stats(subset)['speakers'])
    samples_per_epoch = per_label * (per_label - 1) * n_labels
    samples_per_epoch = samples_per_epoch - (samples_per_epoch % batch_size)

    # actual training
    embedding = SequenceEmbedding(glue=glue)
    embedding.fit(input_shape, architecture,
                  generator, samples_per_epoch, nb_epoch,
                  optimizer=optimizer, log_dir=train_dir)


def generate_test(protocol, subset, feature_extraction, duration):

    np.random.seed(1337)

    # generate set of labeled sequences
    generator = LabeledFixedDurationSequencesBatchGenerator(
        feature_extraction, duration=duration, step=duration, batch_size=-1)
    X, y = zip(*generator(getattr(protocol, subset)()))
    X, y = np.vstack(X), np.hstack(y)

    # randomly select (at most) 100 sequences from each speaker to ensure
    # all speakers have the same importance in the evaluation
    unique, y, counts = np.unique(y, return_inverse=True, return_counts=True)
    n_speakers = len(unique)
    indices = []
    for speaker in range(n_speakers):
        i = np.random.choice(np.where(y == speaker)[0], size=min(100, counts[speaker]), replace=False)
        indices.append(i)
    indices = np.hstack(indices)
    X, y = X[indices], y[indices, np.newaxis]

    return X, y


def tune(protocol, train_dir, tune_dir, beta=1.0, subset='development'):

    batch_size = 32
    os.makedirs(tune_dir)

    architecture_yml = train_dir + '/architecture.yml'
    WEIGHTS_H5 = train_dir + '/weights/{epoch:04d}.h5'

    nb_epoch = 0
    while True:
        weights_h5 = WEIGHTS_H5.format(epoch=nb_epoch)
        if not os.path.isfile(weights_h5):
            break
        nb_epoch += 1

    duration = float(os.path.basename(train_dir))
    config_dir = os.path.dirname(os.path.dirname(os.path.dirname(train_dir)))
    config_yml = config_dir + '/config.yml'
    with open(config_yml, 'r') as fp:
        config = yaml.load(fp)

    # -- FEATURE EXTRACTION --
    feature_extraction_name = config['feature_extraction']['name']
    features = __import__('pyannote.audio.features.yaafe',
                          fromlist=[feature_extraction_name])
    FeatureExtraction = getattr(features, feature_extraction_name)
    feature_extraction = FeatureExtraction(
        **config['feature_extraction'].get('params', {}))

    X, y = generate_test(protocol, subset, feature_extraction, duration)

    alphas = {}

    def objective_function(parameters, beta=1.0):

        epoch = parameters[0]

        weights_h5 = WEIGHTS_H5.format(epoch=epoch)
        sequence_embedding = SequenceEmbedding.from_disk(
            architecture_yml, weights_h5)

        fX = sequence_embedding.transform(X, batch_size=batch_size, verbose=0)

        # compute euclidean distance between every pair of sequences
        y_distance = pdist(fX, metric='euclidean')

        # compute same/different groundtruth
        y_true = pdist(y, metric='chebyshev') < 1

        # false positive / true positive
        fpr, tpr, thresholds = sklearn.metrics.roc_curve(
            y_true, -y_distance, pos_label=True, drop_intermediate=True)

        fnr = 1. - tpr
        far = fpr

        thresholds = -thresholds
        fscore = 1. - f_measure(1. - fnr, 1. - far, beta=beta)

        i = np.nanargmin(fscore)
        alphas[epoch] = float(thresholds[i])
        return fscore[i]

    def callback(res):

        n_trials = len(res.func_vals)

        # save best parameters so far
        epoch = int(res.x[0])
        alpha = alphas[epoch]

        params = {'epoch': epoch,
                  'alpha': alpha}
        with open(tune_dir + '/tune.yml', 'w') as fp:
            yaml.dump(params, fp, default_flow_style=False)

        # plot convergence
        _ = skopt.plots.plot_convergence(res)
        plt.savefig(tune_dir + '/convergence.png', dpi=150)
        plt.close()

        if n_trials % 10 > 0:
            return

        # save results so far
        func = res['specs']['args']['func']
        callback = res['specs']['args']['callback']
        del res['specs']['args']['func']
        del res['specs']['args']['callback']
        skopt.utils.dump(res, tune_dir + '/tune.gz', store_objective=True)
        res['specs']['args']['func'] = func
        res['specs']['args']['callback'] = callback

    epoch = skopt.space.Integer(0, nb_epoch - 1)

    res = skopt.gp_minimize(
        functools.partial(objective_function, beta=beta),
        [epoch, ], callback=callback,
        n_calls=1000, n_random_starts=10,
        x0=[nb_epoch - 1, 0.1],
        random_state=1337, verbose=True)

    return res


def test(protocol, tune_dir, subset='test', beta=1.0):

    batch_size = 32

    train_dir = os.path.dirname(os.path.dirname(tune_dir))

    duration = float(os.path.basename(train_dir))
    config_dir = os.path.dirname(os.path.dirname(os.path.dirname(train_dir)))
    config_yml = config_dir + '/config.yml'
    with open(config_yml, 'r') as fp:
        config = yaml.load(fp)

    # -- FEATURE EXTRACTION --
    feature_extraction_name = config['feature_extraction']['name']
    features = __import__('pyannote.audio.features.yaafe',
                          fromlist=[feature_extraction_name])
    FeatureExtraction = getattr(features, feature_extraction_name)
    feature_extraction = FeatureExtraction(
        **config['feature_extraction'].get('params', {}))

    # -- HYPER-PARAMETERS --
    tune_yml = tune_dir + '/tune.yml'
    with open(tune_yml, 'r') as fp:
        tune = yaml.load(fp)

    architecture_yml = train_dir + '/architecture.yml'
    WEIGHTS_H5 = train_dir + '/weights/{epoch:04d}.h5'
    weights_h5 = WEIGHTS_H5.format(epoch=tune['epoch'])

    sequence_embedding = SequenceEmbedding.from_disk(
        architecture_yml, weights_h5)

    X, y = generate_test(protocol, subset, feature_extraction, duration)
    fX = sequence_embedding.transform(X, batch_size=batch_size, verbose=0)
    y_distance = pdist(fX, metric='euclidean')
    y_true = pdist(y, metric='chebyshev') < 1

    fpr, tpr, thresholds = sklearn.metrics.roc_curve(
        y_true, -y_distance, pos_label=True, drop_intermediate=True)

    frr = 1. - tpr
    far = fpr
    thresholds = -thresholds

    fscore = 1. - f_measure(1. - frr, 1. - far, beta=beta)

    opt_i = np.nanargmin(fscore)
    opt_alpha = float(thresholds[opt_i])
    opt_far = far[opt_i]
    opt_frr = frr[opt_i]
    opt_fscore = fscore[opt_i]

    TEMPLATE = '{condition} {alpha:.5f} {far:.5f} {frr:.5f} {fscore:.5f}'
    print(TEMPLATE.format(condition='optimal',
                          alpha=opt_alpha,
                          far=opt_far,
                          frr=opt_frr,
                          fscore=opt_fscore))

    alpha = tune['alpha']
    actual_i = np.searchsorted(thresholds, alpha)
    actual_far = far[actual_i]
    actual_frr = frr[actual_i]
    actual_fscore = fscore[actual_i]

    print(TEMPLATE.format(condition='actual ',
                          alpha=alpha,
                          far=actual_far,
                          frr=actual_frr,
                          fscore=actual_fscore))


def embed(protocol, tune_dir, apply_dir, subset='test', step=0.1):

    os.makedirs(apply_dir)

    train_dir = os.path.dirname(os.path.dirname(tune_dir))

    duration = float(os.path.basename(train_dir))
    config_dir = os.path.dirname(os.path.dirname(os.path.dirname(train_dir)))
    config_yml = config_dir + '/config.yml'
    with open(config_yml, 'r') as fp:
        config = yaml.load(fp)

    # -- FEATURE EXTRACTION --
    feature_extraction_name = config['feature_extraction']['name']
    features = __import__('pyannote.audio.features.yaafe',
                          fromlist=[feature_extraction_name])
    FeatureExtraction = getattr(features, feature_extraction_name)
    feature_extraction = FeatureExtraction(
        **config['feature_extraction'].get('params', {}))

    # -- HYPER-PARAMETERS --
    tune_yml = tune_dir + '/tune.yml'
    with open(tune_yml, 'r') as fp:
        tune = yaml.load(fp)

    architecture_yml = train_dir + '/architecture.yml'
    WEIGHTS_H5 = train_dir + '/weights/{epoch:04d}.h5'
    weights_h5 = WEIGHTS_H5.format(epoch=tune['epoch'])

    sequence_embedding = SequenceEmbedding.from_disk(
        architecture_yml, weights_h5)

    extraction = Extraction(sequence_embedding,
                            feature_extraction,
                            duration=duration,
                            step=step)
    EMBED_PKL = apply_dir + '/{uri}.pkl'

    for test_file in getattr(protocol, subset)():
        wav = test_file['medium']['wav']
        uri = test_file['uri']
        embedding = extraction.apply(wav)
        with open(EMBED_PKL.format(uri=uri), 'w') as fp:
            pickle.dump(embedding, fp)

if __name__ == '__main__':

    arguments = docopt(__doc__, version='Speaker embedding')

    medium_template = {}
    if '<wav_template>' in arguments:
        medium_template = {'wav': arguments['<wav_template>']}

    if '<database.task.protocol>' in arguments:
        protocol = arguments['<database.task.protocol>']
        database_name, task_name, protocol_name = protocol.split('.')
        database = get_database(database_name, medium_template=medium_template)
        protocol = database.get_protocol(task_name, protocol_name)

    subset = arguments['--subset']

    arguments = docopt(__doc__, version='Speaker embedding')

    if arguments['train']:
        experiment_dir = arguments['<experiment_dir>']
        if subset is None:
            subset = 'train'
        duration = float(arguments['--duration'])
        TRAIN_DIR = '{experiment_dir}/train/{protocol}.{subset}/{duration:g}'
        train_dir = TRAIN_DIR.format(
            experiment_dir=experiment_dir,
            protocol=arguments['<database.task.protocol>'],
            subset=subset, duration=duration)
        train(protocol, duration, experiment_dir, train_dir, subset=subset)

    if arguments['tune']:
        train_dir = arguments['<train_dir>']
        if subset is None:
            subset = 'development'
        beta = float(arguments['--false-alarm'])
        tune_dir = train_dir + '/tune/' + arguments['<database.task.protocol>'] + '.' + subset
        res = tune(protocol, train_dir, tune_dir, beta=beta, subset=subset)

    if arguments['test']:
        tune_dir = arguments['<tune_dir>']
        if subset is None:
            subset = 'test'
        beta = float(arguments['--false-alarm'])
        test(protocol, tune_dir, subset=subset, beta=beta)

    if arguments['apply']:
        tune_dir = arguments['<tune_dir>']
        if subset is None:
            subset = 'test'
        apply_dir = tune_dir + '/apply/' + arguments['<database.task.protocol>'] + '.' + subset
        step = float(arguments['--step'])
        embed(protocol, tune_dir, apply_dir, subset=subset, step=step)
