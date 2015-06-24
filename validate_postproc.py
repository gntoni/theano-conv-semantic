"""
Functions for testing conv net, training and testing on just one image
Version that works with Conv that uses randomization on beginning
"""
import sys
import time
import logging

import numpy as np

import theano
import theano.tensor as T

from helpers.data_helper import shared_dataset
from helpers.build_multiscale import build_multiscale, extend_net_w1l, extend_net_w1l_drop
# from preprocessing.transform_out import resize_marked_image
from util import try_pickle_load
from helpers.load_conf import load_config
from helpers.eval import calc_class_accuracy
from scipy.ndimage import zoom
from cv2 import imread
from postprocessing.superpixel import segment
import matplotlib.pyplot as plt
import matplotlib.colors

logger = logging.getLogger(__name__)

ReLU = lambda x: T.maximum(x, 0)
lReLU = lambda x: T.maximum(x, 1./5*x)  # leaky ReLU

IMGS = "./data/iccv09Data/images/"
SHAPE = (60, 80)


def set_layers_training_mode(layers, mode):
    """
    Sets training mode in layers which support traning_mode
    """
    for i, layer in enumerate(layers):
        if 'training_mode' in layer.__dict__:
            # logger.info('Found layer with trainig mode %d, setting to %d',
            #              i, mode)
            layer.training_mode.set_value(mode)


def load_orig_img(name):
    img = imread(IMGS + name + ".jpg")
    return img


def oversegment(orig_img, marked):
    segmented = segment(orig_img, 0.5, 250, 40)
    # crop to real size
    segmented = segmented[:240, :320]
    """
    # DEBUG
    plt.subplot(2, 1, 1)
    plt.axis('off')
    plt.imshow(orig_img)
    plt.subplot(2, 1, 2)
    plt.axis('off')
    plt.imshow(segmented, cmap=plt.get_cmap('Paired'))
    plt.show()
    plt.draw()
    """
    new_mark = np.copy(marked)
    for i in np.unique(segmented):
        curr_marks = marked[segmented == i]
        most_freq = np.bincount(curr_marks).argmax()
        new_mark[segmented == i] = most_freq

    return new_mark


def print_stats(results, y, n_classes, dont_care_classes,
                fnames_path, show=False, postproc=None):
    assert(len(results) == y.shape[0])
    fnames = []
    with open(fnames_path, 'r') as f:
        fnames = f.read().split('\n')

    care_classes = np.ones((n_classes), dtype='int8')
    care_classes[dont_care_classes] = 0

    correct = np.zeros((n_classes), dtype='int32')
    total = np.zeros((n_classes), dtype='int32')
    for ind, img in enumerate(results):
        curr_y = y[ind]
        img_up = zoom(img, 4, order=0)
        img_old = img_up
        assert(img_up.shape[0] == 240)

        orig_img = load_orig_img(fnames[ind])
        # apply postprocessing
        if postproc is not None:
            img_up = postproc(orig_img, img_up)

        if show:
            bounds = np.linspace(0, 30, 31)
            # cmap = plt.get_cmap('Paired')
            cmap = plt.get_cmap('gist_ncar')
            norm = matplotlib.colors.BoundaryNorm(bounds, cmap.N)
            plt.subplot(2, 2, 1)
            plt.axis('off')
            plt.imshow(orig_img)
            plt.subplot(2, 2, 2)
            plt.axis('off')
            plt.imshow(curr_y, cmap=cmap, norm=norm)
            plt.subplot(2, 2, 3)
            plt.axis('off')
            plt.imshow(img_old, cmap=cmap, norm=norm)
            plt.subplot(2, 2, 4)
            plt.axis('off')
            plt.imshow(img_up, cmap=cmap, norm=norm)
            plt.show()
            plt.draw()

        # count correct pixels
        for i in range(n_classes):
            if np.any(curr_y == i):
                correct[i] += np.sum(np.equal(img_up[curr_y == i],
                                              curr_y[curr_y == i]))
                total[i] += np.sum(curr_y == i)

    care_correct = correct * care_classes
    care_total = total * care_classes
    class_accuracy = calc_class_accuracy(care_correct, care_total)

    logger.info('total pixel accuracy %f %%',
                (np.sum(care_correct, dtype='float32') /
                 np.sum(care_total)) * 100.)
    logger.info('mean class accuracy: %f %%',
                class_accuracy * 100.)
    logger.info('per class accuracies: %s',
                correct.astype('float32') / total)


def validate(conf, net_weights):

    logger.info("... loading data")
    logger.debug("Theano.config.floatX is %s" % theano.config.floatX)

    path = conf['data']['location']
    batch_size = 1
    assert(type(batch_size) is int)
    logger.info('Batch size %d' % (batch_size))

    try:
        x_train_allscales = try_pickle_load(path + 'x_train.bin')
        x_train = x_train_allscales[0]  # first scale
        y_train = try_pickle_load(path + 'y_train.bin')
        x_test_allscales = try_pickle_load(path + 'x_test.bin')
        x_test = x_test_allscales[0]
        y_test = try_pickle_load(path + 'y_test.bin')
    except IOError:
        logger.error("Unable to load Theano dataset from %s", path)
        exit(1)

    n_classes = int(max(y_train.max(), y_test.max()) + 1)
    logger.info("Dataset has %d classes", n_classes)

    image_shape = (x_train.shape[-2], x_train.shape[-1])
    logger.info("Image shape is %s", image_shape)

    logger.info('Train set has %d images' %
                x_train.shape[0])
    logger.info('Input train set has shape of %s ',
                x_train.shape)
    logger.info('Test set has %d images' %
                x_test.shape[0])
    logger.info('Input test set has shape of %s ',
                x_test.shape)

    # compute number of minibatches for training, validation and testing
    n_train_batches = x_train.shape[0] // batch_size
    n_test_batches = x_test.shape[0] // batch_size

    logger.info("Number of train batches %d" % n_train_batches)
    logger.info("Number of test batches %d" % n_test_batches)

    logger.info("... building network")

    # allocate symbolic variables for the data
    index = T.lscalar()  # index to a [mini]batch

    # input is presented as (batch, channel, x, y)
    x0 = T.tensor4('x')
    x2 = T.tensor4('x')
    x4 = T.tensor4('x')
    # matrix row - batch index, column label of pixel
    # every column is a list of pixel labels (image matrix reshaped to list)
    y = T.imatrix('y')

    # create all layers
    layers, out_shape, conv_out = build_multiscale(
        x0, x2, x4, y, batch_size, classes=n_classes,
        image_shape=image_shape,
        nkerns=[16, 64, 256],
        activation=lReLU, bias=0.001,
        sparse=False)
    logger.info("Image out shape is %s", out_shape)

    # last layer, log reg
    # y_flat = y.flatten(1)

    y_train_shape = (y_train.shape[0], out_shape[0], out_shape[1])
    y_test_shape = (y_test.shape[0], out_shape[0], out_shape[1])

    # resize marked images to out_size of the network
    y_train_downscaled = np.empty(y_train_shape)
    # for i in xrange(y_train.shape[0]):
    #     y_train_downscaled[i] = resize_marked_image(y_train[i], out_shape)

    # resize marked images to out_size of the network
    y_test_downscaled = np.empty(y_test_shape)
    # for i in xrange(y_test.shape[0]):
    #     y_test_downscaled[i] = resize_marked_image(y_test[i], out_shape)

    x_train_shared, y_train_shared = \
        shared_dataset((x_train,
                        y_train_downscaled))
    x2_train_shared = theano.shared(x_train_allscales[1], borrow=True)
    x4_train_shared = theano.shared(x_train_allscales[2], borrow=True)

    x_test_shared, y_test_shared = \
        shared_dataset((x_test,
                        y_test_downscaled))
    x2_test_shared = theano.shared(x_test_allscales[1], borrow=True)
    x4_test_shared = theano.shared(x_test_allscales[2], borrow=True)

    # When storing data on the GPU it has to be stored as floats
    # therefore we will store the labels as ``floatX`` as well
    # (``shared_y`` does exactly that). But during our computations
    # we need them as ints (we use labels as index, and if they are
    # floats it doesn't make sense) therefore instead of returning
    # ``shared_y`` we will have to cast it to int. This little hack
    # lets ous get around this issue
    # y_train_shared_i32 = T.cast(y_train_shared, 'int32')
    # y_test_shared_i32 = T.cast(y_test_shared, 'int32')

    ###############
    # BUILD MODEL #
    ###############
    logger.info("... building model")

    layers, new_layers = extend_net_w1l_drop(
        conv_out, layers, n_classes,
        nkerns=[1000],
        activation=lReLU, bias=0.001)

    test_model_trainset = theano.function(
        [index],
        [layers[0].y_pred],
        givens={
            x0: x_train_shared[index * batch_size: (index + 1) * batch_size],
            x2: x2_train_shared[index * batch_size: (index + 1) * batch_size],
            x4: x4_train_shared[index * batch_size: (index + 1) * batch_size]
            # y: y_train_shared_i32[index * batch_size: (index + 1) * batch_size]
        }
    )
    test_model_testset = theano.function(
        [index],
        [layers[0].y_pred],
        givens={
            x0: x_test_shared[index * batch_size: (index + 1) * batch_size],
            x2: x2_test_shared[index * batch_size: (index + 1) * batch_size],
            x4: x4_test_shared[index * batch_size: (index + 1) * batch_size]
            # y: y_test_shared_i32[index * batch_size: (index + 1) * batch_size]
        }
    )

    # try to load weights
    try:
        if net_weights is not None:
            for net_weight, layer in zip(net_weights, layers):
                layer.set_weights(net_weight)
            logger.info("Loaded net weights from file.")
            net_weights = None
    except:
        logger.error("Uncompatible network to load weights in")
        exit(1)

    set_layers_training_mode(layers, 0)

    '''
    logger.info("---> Train set")
    start_time = time.clock()
    validation = [test_model_trainset(i)[0].reshape(SHAPE)
                  for i in xrange(n_train_batches)]
    end_time = time.clock()
    logger.info("Validated %d images in %.2f seconds",
                n_train_batches, end_time - start_time)
    print_stats(validation, y_train, layers[0].n_classes,
                conf['data']['dont-care-classes'], 'samples_train.log')
    '''

    logger.info("---> Test set - no postprocessing")
    start_time = time.clock()
    validation = [test_model_testset(i)[0].reshape(SHAPE)
                  for i in xrange(n_test_batches)]
    end_time = time.clock()
    logger.info("Validated %d images in %.2f seconds",
                n_train_batches, end_time - start_time)
    print_stats(validation, y_test, layers[0].n_classes,
                conf['data']['dont-care-classes'], 'samples_test.log')

    logger.info("---> Test set - segmentation")
    print_stats(validation, y_test, layers[0].n_classes,
                conf['data']['dont-care-classes'], 'samples_test.log',
                postproc=oversegment, show=False)



if __name__ == '__main__':
    """
    Examples of usage:
    python validate.py network.conf network-12-34.bin
        validates network
    """
    logging.basicConfig(level=logging.INFO)

    argc = len(sys.argv)
    if argc == 3:
        net_config_path = sys.argv[1]
        params = try_pickle_load(sys.argv[2])
        if params is None:
            exit(1)
    else:
        logger.error("Too few arguments")
        exit(1)

    conf = load_config(net_config_path)
    if conf is None:
        exit(1)

    #   run evaluation
    validate(conf, params)