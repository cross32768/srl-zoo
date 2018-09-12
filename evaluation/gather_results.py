"""
Create csv result from a folder of experiments
"""
from __future__ import print_function, division, absolute_import

import argparse
import json
import os

import pandas as pd

from pipeline import correlationCall, knnCall


def getKnnMse(path):
    """
    Retrieve knn mse score
    :param path: (str)
    :return: (float)
    """
    try:
        with open(path) as f:
            return json.load(f)['knn_mse']
    except IOError:
        print("knn_mse.json not found for {}".format(path))
        return -1

def getCorrelation(path):
    """
    Retrieve Ground Truth Correlation
    :param path: (str)
    :return: (float, [float])
    """
    try:
        with open(path) as f:
            results = json.load(f)
            return results['gt_corr_mean'], results['gt_corr']
    except IOError:
        print("gt_correlation.json not found for {}".format(path))
        return -1, [-1]


parser = argparse.ArgumentParser(description='Create a report file for a given dataset')
parser.add_argument('-i', '--log-dir', type=str, default="", required=True, help='Path to a dataset log folder')
args = parser.parse_args()

assert os.path.isdir(args.log_dir), "--log-dir must be a path to a valid folder"

log_dir = args.log_dir
experiments = []
for item in os.listdir(log_dir):
    if 'baselines' not in item and os.path.isdir('{}/{}'.format(log_dir, item)):
        experiments.append(item)

experiments.append(*['baselines/' + f for f in os.listdir(log_dir + "/baselines")])

experiments.sort()
print("Found {} experiments".format(len(experiments)))

knn_mse = []
gt_corr, gt_mean = [], []

# Add here keys from exp_config.json that should be saved in the csv report file
exp_configs = {'training-set-size': [], 'split-dimensions': [], 'state-dim': [], 'seed': []}

for experiment in experiments:

    skip = False
    try:
        with open('{}/{}/exp_config.json'.format(log_dir, experiment)) as f:
            exp_config = json.load(f)
    except FileNotFoundError:
        print("exp_config not found for {}".format(experiment))
        skip = True
        exp_config = {}

    for key in exp_configs.keys():
        exp_configs[key].append(exp_config.get(key, None))

    get_corr_file = '{}/{}/gt_correlation.json'.format(log_dir, experiment)
    if not os.path.isfile(get_corr_file) and not skip:
        try:
            correlationCall(exp_config, plot=False)
        except RuntimeError:
            # TODO: compute states_rewards.npz if not found
            pass
    gt_correlation = getCorrelation(get_corr_file)
    gt_mean.append(gt_correlation[0])
    gt_corr.append(gt_correlation[1])

    knn_mse_file = '{}/{}/knn_mse.json'.format(log_dir, experiment)
    if not os.path.isfile(knn_mse_file) and not skip:
        try:
            knnCall(exp_config)
        except RuntimeError:
            # TODO: compute states_rewards.npz if not found
            pass

    knn_mse.append(getKnnMse(knn_mse_file))


exp_configs.update({'experiments': experiments, 'knn_mse': knn_mse,
                    'gt_corr': gt_corr, 'gt_mean': gt_mean})

result_df = pd.DataFrame(exp_configs)
result_df.to_csv('{}/results.csv'.format(log_dir), sep=",", index=False)
print("Saved results to {}/results.csv".format(log_dir))
