from __future__ import print_function, division, absolute_import

import glob
import random
import time

# Python 2/3 support
try:
    import queue
except ImportError:
    import Queue as queue

import cv2
import numpy as np
import torch as th
from joblib import Parallel, delayed
from torch.multiprocessing import Queue, Process

from .preprocess import IMAGE_WIDTH, IMAGE_HEIGHT
from .utils import preprocessInput


def preprocessImage(image, convert_to_rgb=True):
    """
    :param image: (numpy matrix) BGR image
    :param convert_to_rgb: (bool) whether the conversion to rgb is needed or not
    :return: (numpy matrix)
    """
    # Resize
    im = cv2.resize(image, (IMAGE_WIDTH, IMAGE_HEIGHT), interpolation=cv2.INTER_AREA)
    # Convert BGR to RGB
    if convert_to_rgb:
        im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
    # Normalize
    im = preprocessInput(im.astype(np.float32), mode="image_net")
    return im


class DataLoader(object):
    def __init__(self, minibatchlist, images_path, n_workers=1, multi_view=False, use_triplets=False,
                 infinite_loop=True, max_queue_len=4, is_training=False):
        """
        A Custom dataloader to work with our datasets, and to prepare data for the different models
        (inverse, priors, autoencoder, ...)
        :param minibatchlist: ([np.array]) list of observations indices (grouped per minibatch)
        :param images_path: (np.array) Array of path to images
        :param n_workers: (int) number of preprocessing worker (load and preprocess each image)
        :param multi_view: (bool)
        :param use_triplets: (bool)
        :param infinite_loop: (bool) whether to have an iterator that can be resetted, set to False, it
        :param max_queue_len: (int) Max number of minibatches that can be preprocessed at the same time
        :param is_training: (bool)
            Set to True, the dataloader will output both `obs` and `next_obs` (a tuple of th.Tensor)
            Set to false, it will only output one th.Tensor.
        """
        super(DataLoader, self).__init__()
        self.n_workers = n_workers
        self.infinite_loop = infinite_loop
        self.n_minibatches = len(minibatchlist)
        self.minibatchlist = minibatchlist
        self.images_path = images_path
        self.shuffle = is_training
        self.queue = Queue(max_queue_len)
        self.process = None
        self.use_triplets = use_triplets
        self.multi_view = multi_view
        self.startProcess()

    @staticmethod
    def createTestMinibatchList(n_samples, batch_size):
        """
        Create list of minibatch for plotting
        :param n_samples: (int)
        :param batch_size: (int)
        :return: ([np.array])
        """
        minibatchlist = []
        for i in range(n_samples // batch_size + 1):
            start_idx = i * batch_size
            end_idx = min(n_samples, (i + 1) * batch_size)
            minibatchlist.append(np.arange(start_idx, end_idx))
        return minibatchlist

    def startProcess(self):
        """Start preprocessing process"""
        self.process = Process(target=self._run)
        # Make it a deamon, so it will be deleted at the same time
        # of the main process
        self.process.daemon = True
        self.process.start()

    def _run(self):
        start = True
        with Parallel(n_jobs=self.n_workers, batch_size="auto", backend="threading") as parallel:
            while start or self.infinite_loop:
                start = False
                if self.shuffle:
                    indices = np.random.permutation(self.n_minibatches).astype(np.int64)
                else:
                    indices = np.arange(len(self.minibatchlist), dtype=np.int64)

                for minibatch_idx in indices:
                    if self.shuffle:
                        images = np.stack((self.images_path[self.minibatchlist[minibatch_idx]],
                                           self.images_path[self.minibatchlist[minibatch_idx] + 1]))
                        images = images.flatten()
                        # print(images)
                    else:
                        images = self.images_path[self.minibatchlist[minibatch_idx]]

                    if self.n_workers <= 1:
                        batch = [self._makeBatchElement(image_path, self.multi_view, self.use_triplets) for image_path
                                 in images]
                    else:
                        batch = parallel(
                            delayed(self._makeBatchElement)(image_path, self.multi_view, self.use_triplets) for
                            image_path in images)

                    batch = th.cat(batch, dim=0)

                    if self.shuffle:
                        batch_obs, batch_next_obs = batch[:len(images) // 2], batch[len(images) // 2:]
                        self.queue.put((minibatch_idx, batch_obs, batch_next_obs))
                    else:
                        self.queue.put(batch)

                    # Free memory
                    if self.shuffle:
                        del batch_obs
                        del batch_next_obs
                    del batch

                self.queue.put(None)

    @classmethod
    def _makeBatchElement(cls, image_path, multi_view=False, use_triplets=False):
        """
        :param image_path: (str) path to an image (without the 'data/' prefix)
        :param multi_view: (bool)
        :param use_triplets: (bool)
        :return: (th.Tensor)
        """
        # Remove trailing .jpg if present
        image_path = 'data/' + image_path.split('.jpg')[0]

        if multi_view:
            images = []
            # Load different view of the same timestep
            for i in range(2):
                im = cv2.imread("{}_{}.jpg".format(image_path, i + 1))
                if im is None:
                    raise ValueError("tried to load {}_{}.jpg, but it was not found".format(image_path, i + 1))
                images.append(preprocessImage(im))

            # loading a negative observation
            if use_triplets:
                # End of file format for positive & negative observations (camera 1) - length : 6 characters
                extra_chars = '_1.jpg'

                # getting path for all files of same record episode, e.g path_to_data/record_001/frame[0-9]{6}*
                digits_path = glob.glob(image_path[:-6] + '[0-9]*' + extra_chars)

                # getting the current & all frames' timesteps
                current = int(image_path[-6:])
                # For all others extract last 6 digits (timestep) after removing the extra chars
                all_frame_steps = [int(k[:-len(extra_chars)][-6:]) for k in digits_path]
                # removing current positive timestep from the list
                all_frame_steps.remove(current)

                # negative timestep by random sampling
                length_set_steps = len(all_frame_steps)
                negative = all_frame_steps[random.randint(0, length_set_steps - 1)]
                negative_path = '{}{:06d}'.format(image_path[:-6], negative)

                im3 = cv2.imread(negative_path + "_1.jpg")
                if im3 is None:
                    raise ValueError("tried to load {}_{}.jpg, but it was not found".format(negative_path, 1))
                im3 = preprocessImage(im3)
                # stacking along channels
                images.append(im3)

            im = np.dstack(images)

        else:
            im = cv2.imread("{}.jpg".format(image_path))
            if im is None:
                raise ValueError("tried to load {}.jpg, but it was not found".format(image_path))
            im = preprocessImage(im)

        # Channel first (for pytorch convolutions) + one dim for the batch
        # th.tensor creates a copy
        im = th.tensor(im.reshape((1,) + im.shape).transpose(0, 3, 2, 1))
        return im

    def __len__(self):
        return self.n_minibatches

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            try:
                val = self.queue.get_nowait()
                break
            except queue.Empty:
                time.sleep(0.001)
                continue
        if val is None:
            raise StopIteration
        return val

    next = __next__  # Python 2 compatibility

    def __del__(self):
        if self.process is not None:
            self.process.terminate()


class SupervisedDataLoader(DataLoader):
    """
    Data loader for supervised learning.
    :param x_indices: (np.array) indices of observations
    :param y_values: (np.array) targets for each input value
    :param images_path: (np.array) Array of path to images
    :param batch_size: (int)
    :param n_workers: (int) number of workers used for preprocessing
    :param no_targets: (bool) Set to true, only inputs are generated
    :param shuffle: (bool) Set to True, the dataloader will shuffle the indices
    :param infinite_loop: (bool) whether to have an iterator that can be resetted, set to False, it
    :param max_queue_len: (int) Max number of minibatches that can be preprocessed at the same time
    """

    def __init__(self, x_indices, y_values, images_path, batch_size, n_workers=1, no_targets=False,
                 shuffle=False, infinite_loop=True, max_queue_len=4):
        # Create minibatch list
        minibatchlist, targets = self.createMinibatchList(x_indices, y_values, batch_size)

        # Whether to yield targets together with output
        # (not needed when plotting or predicting states)
        self.no_targets = no_targets
        self.targets = np.array(targets)
        self.shuffle = shuffle
        super(SupervisedDataLoader, self).__init__(minibatchlist, images_path, n_workers=n_workers,
                                                   infinite_loop=infinite_loop, max_queue_len=max_queue_len)

    def _run(self):
        start = True
        with Parallel(n_jobs=self.n_workers, batch_size="auto", backend="threading") as parallel:
            while start or self.infinite_loop:
                start = False
                if self.shuffle:
                    indices = np.random.permutation(self.n_minibatches).astype(np.int64)
                else:
                    indices = np.arange(len(self.minibatchlist), dtype=np.int64)

                for minibatch_idx in indices:
                    images = self.images_path[self.minibatchlist[minibatch_idx]]

                    if self.n_workers <= 1:
                        batch = [self._makeBatchElement(image_path) for image_path in images]
                    else:
                        batch = parallel(delayed(self._makeBatchElement)(image_path) for image_path in images)

                    batch = th.cat(batch, dim=0)

                    if self.no_targets:
                        self.queue.put(batch)
                    else:
                        # th.tensor creates a copy
                        self.queue.put((batch, th.tensor(self.targets[minibatch_idx])))

                    # Free memory
                    del batch

                self.queue.put(None)

    @staticmethod
    def createMinibatchList(x_indices, y_values, batch_size):
        """
        Create list of minibatches (contains the observations indices)
        along with the corresponding list of targets
        Warning: this may create minibatches of different length
        :param x_indices: (np.array)
        :param y_values: (np.array)
        :param batch_size: (int)
        :return: ([np.array], [np.array])
        """
        targets = []
        minibatchlist = []
        n_minibatches = len(x_indices) // batch_size + 1
        for i in range(0, n_minibatches):
            start_idx = i * batch_size
            end_idx = min(start_idx + batch_size, len(x_indices))
            excerpt = slice(start_idx, end_idx)
            # Remove excerpt with no elements
            if len(x_indices[excerpt]) > 0:
                minibatchlist.append(x_indices[excerpt])
                targets.append(y_values[excerpt])

        return minibatchlist, targets
