#!/usr/bin/env python3

import os
import sys
import time
import numpy as np
import scipy as sp
import matplotlib as mpl
import matplotlib.pyplot as plt
from pathlib import Path
import fid
from scipy.misc import imsave

def print_info(msg, do_print, flush=True):
    if do_print:
        print(msg, flush=flush)


def sample_images(img_tensor, sess, n_batches, path=None):
    images = [sess.run(img_tensor) for i in range(n_batches)]
    images = np.vstack(images)
    if path:
        path = Path(path)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        for i, img in enumerate(images):
            imsave(path / ("%05d.png" % i), img)
    return images


def run(dataset, generator_type, discriminator_type, latentsize, kernel_dimension, epsilon, learning_rate, batch_size, options, logdir_base='/tmp'):
    tf.reset_default_graph()
    dtype = tf.float32

    run_name = '_'.join(['%s' % get_timestamp(),
        'g%s' % generator_type,
        'd%s' % discriminator_type,
        'z%d' % latentsize,
        'l%1.0e' % learning_rate,
        'l2p%1.0e' % options.l2_penalty,
        'd%d' % kernel_dimension,
        'eps%3.2f' % epsilon,
        'lds%1.e' % options.discriminator_lr_scale,
    ])
    run_name += ("_l2pscale%1.e" % options.gen_l2p_scale) if options.gen_l2p_scale != 1.0 else ''
    run_name += "_M" if options.remember_previous else ''
    run_name += ("_dl%s" % options.disc_loss) if options.disc_loss != 'l2' else ''
    run_name += ("_%s" % options.logdir_suffix) if options.logdir_suffix else ''

    if options.verbosity == 0:
        tf.logging.set_verbosity(tf.logging.ERROR)

    subdir = "%s_%s" % (get_timestamp('%y%m%d'), dataset)
    logdir = Path(logdir_base) / subdir / run_name
    print_info("\nLogdir: %s\n" % logdir, options.verbosity > 0)
    if __name__ == "__main__" and options.sample_images is None:
        startup_bookkeeping(logdir, __file__)
        trainlog = open(logdir / 'logfile.csv', 'w')
    else:
        trainlog = None

    # We use the tanh as suggest in in LeCun's "Efficient Backprop", so that
    # 0/1 aren't the saturation-endpoints of the output but are easily reachable.
    # TODO: not sure if this is useful or not?
    out_fn = lambda x : 1.7159 * tf.nn.tanh(x * 2.0 / 3.0)

    dataset_pattern, n_samples, img_shape = get_dataset_path(dataset)
    z = tf.random_normal([batch_size, latentsize], dtype=dtype, name="z")
    x = create_generator(z, img_shape, options.l2_penalty*options.gen_l2p_scale, generator_type, batch_size, out_fn=out_fn)
    y = load_dataset(dataset_pattern, batch_size, img_shape, n_threads=options.threads)
    assert x.get_shape().as_list()[1:] == y.get_shape().as_list()[1:], "X and Y have different shapes: %s vs %s" % (x.get_shape().as_list(), y.get_shape().as_list())

    disc_x = create_discriminator(x, discriminator_type, options.l2_penalty, False)
    disc_y = create_discriminator(y, discriminator_type, options.l2_penalty, True)

    x_old = tf.get_variable("x_old", shape=x.shape, initializer=tf.zeros_initializer(), trainable=False)
    disc_x_old = create_discriminator(x_old, discriminator_type, options.l2_penalty, True)
    disc_x_old = tf.reshape(disc_x_old, [-1])

    with tf.name_scope('loss'):
        disc_x = tf.reshape(disc_x, [-1])
        disc_y = tf.reshape(disc_y, [-1])
        pot_x, pot_y = get_potentials(x, y, kernel_dimension, epsilon)
        pot_x_old = calculate_potential(x, y, x_old, kernel_dimension, epsilon)

        if options.disc_loss == 'l2':
            disc_loss_fn = tf.losses.mean_squared_error
        elif options.disc_loss == 'l1':
            disc_loss_fn = tf.losses.absolute_difference
        else:
            assert False, "Unknown Discriminator Loss: %s" % options.disc_loss

        loss_d_x = disc_loss_fn(pot_x, disc_x)
        loss_d_y = disc_loss_fn(pot_y, disc_y)
        loss_d = loss_d_x + loss_d_y
        loss_g = tf.reduce_mean(disc_x)

        if options.remember_previous:
            loss_d_x_old = disc_loss_fn(pot_x_old, disc_x_old)
            loss_d += loss_d_x_old

    vars_d = [v for v in tf.global_variables() if v.name.startswith('discriminator')]
    vars_g = [v for v in tf.global_variables() if v.name.startswith('generator')]
    optim_d = tf.train.AdamOptimizer(learning_rate*options.discriminator_lr_scale)
    optim_g = tf.train.AdamOptimizer(learning_rate)

    # we can sum all regularizers in one term, the var-list argument to minimize
    # should make sure each optimizer only regularizes "its own" variables
    regularizers = tf.reduce_sum(tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))
    train_op_d = optim_d.minimize(loss_d  + regularizers, var_list=vars_d)
    train_op_g = optim_g.minimize(loss_g + regularizers, var_list=vars_g)
    train_op = tf.group(train_op_d, train_op_g)

    if options.remember_previous:
        with tf.control_dependencies([train_op]):
            assign_x_op = tf.assign(x_old, x)
        train_op = tf.group(train_op, assign_x_op)

    # Tensorboard summaries
    x_img = (tf.clip_by_value(x, -1.0, 1.0) + 1) / 2.0
    y_img = tf.clip_by_value((y + 1) / 2, 0.0, 1.0)
    if options.create_summaries:
        with tf.name_scope("distances"):
            tf.summary.histogram("xx", generate_all_distances(x, x))
            tf.summary.histogram("xy", generate_all_distances(x, y))
            tf.summary.histogram("yy", generate_all_distances(y, y))
        with tf.name_scope('discriminator_stats'):
            tf.summary.histogram('output_x', disc_x)
            tf.summary.histogram('output_y', disc_y)
            tf.summary.histogram('pred_error_y', pot_y - disc_y)
            tf.summary.histogram('pred_error_x', pot_x - disc_x)
        with tf.name_scope('potential'):
            tf.summary.histogram('x', pot_x)
            tf.summary.histogram('y', pot_y)
            if options.remember_previous:
                tf.summary.histogram('x_old', pot_x_old)
        img_smry = tf.summary.image("out_img", x_img, 2)
        img_smry = tf.summary.image("in_img", y_img, 2)
        with tf.name_scope("losses"):
            tf.summary.scalar('loss_d_x', loss_d_x)
            tf.summary.scalar('loss_d_y', loss_d_y)
            tf.summary.scalar('loss_d', loss_d)
            tf.summary.scalar('loss_g', loss_g)

        with tf.name_scope('weightnorm'):
            for v in tf.global_variables():
                if not v.name.endswith('kernel:0'):
                    continue
                tf.summary.scalar("wn_"+v.name[:-8], tf.norm(v))
        with tf.name_scope('mean_activations'):
            for op in tf.get_default_graph().get_operations():
                if not op.name.endswith('Tanh'):
                    continue
                tf.summary.scalar("act_"+op.name, tf.reduce_mean(op.outputs[0]))
    merged_smry = tf.summary.merge_all()

    fid_stats_file = options.fid_stats % dataset.lower()
    assert pathlib.Path(fid_stats_file).exists(), "Can't find training set statistics for FID"
    f = np.load(fid_stats_file)
    mu_fid, sigma_fid = f['mu'][:], f['sigma'][:]
    f.close()
    inception_path = fid.check_or_download_inception(options.inception_path)
    fid.create_inception_graph(inception_path)

    maxv = 0.05
    cmap = plt.cm.ScalarMappable(mpl.colors.Normalize(-maxv, maxv), cmap=plt.cm.RdBu)
    config = tf.ConfigProto(intra_op_parallelism_threads=2,
                            inter_op_parallelism_threads=2,
                            use_per_session_threads=True,
                            gpu_options = tf.GPUOptions(allow_growth=True))
    with tf.Session(config=config) as sess:
        log = tf.summary.FileWriter(str(logdir), sess.graph)
        sess.run(tf.global_variables_initializer())
        if options.resume_checkpoint:
            vars_resume = [v for v in tf.global_variables() if v.name.startswith('generator')]
            vars_resume += [v for v in tf.global_variables() if v.name.startswith('discriminator')]
            loader = tf.train.Saver(vars_resume)
            loader.restore(sess, options.resume_checkpoint)
        sess.run(tf.local_variables_initializer())
        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(coord=coord)
        fd = {}

        if options.sample_images is not None:
            n_batches = (1 + options.sample_images_size // batch_size)
            sample_images(x_img, sess, n_batches, path=options.sample_images)
            coord.request_stop()
            coord.join(threads)
            return

        saver = tf.train.Saver(max_to_keep=50)
        max_iter = int(options.iterations * 1000)

        n_epochs = max_iter / (n_samples / batch_size)
        print_info("total iterations: %d (= %3.2f epochs)" % (max_iter, n_epochs), options.verbosity > 0)
        t0 = time.time()
        for cur_iter in range(max_iter+1): # +1 so we are more likely to get a model/stats line at the end

            sess.run(train_op)
            print_stats = cur_iter % 5000 == 0 #or (cur_iter < 1000 and cur_iter % 100 == 0) or (cur_iter == max_iter - 1)
            if print_stats:
                smry, xx_img = sess.run([merged_smry, x_img])
                log.add_summary(smry, cur_iter)

                images = sample_images(x_img, sess, n_batches=5*1024 // batch_size)*255
                mu_gen, sigma_gen = fid.calculate_activation_statistics(images, sess, batch_size=128)
                fid_value = fid.calculate_frechet_distance(mu_gen, sigma_gen, mu_fid, sigma_fid)

                s = (cur_iter, fid_value, time.time() - t0, dataset, run_name)
                fig = plot_tiles(xx_img, 10, 10, local_norm="none", figsize=(6.6, 6.6))
                fig.savefig(str(logdir / ('%09d.png' % cur_iter)))
                plt.close(fig)
                if (cur_iter > 0) and (cur_iter % options.checkpoint_every == 0):
                    saver.save(sess, str(logdir / 'model'), global_step=cur_iter)
                if trainlog:
                    print(', '.join([str(ss) for ss in s]), file=trainlog, flush=True)
                print_info("%9d  %3.2f -- %3.2fs %s %s" % s, options.verbosity > 0)

        if trainlog:
            trainlog.close()
        coord.request_stop()
        coord.join(threads)
        return


def setup_argumentparser():
    default_logdir = "/publicwork/coulomb_gan/"
    from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
    parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument("-i", '-e', "--iterations", type=int, help='number of SGD updates (in thousand)', default=500)
    parser.add_argument("-b", "--batch_size", type=int, help='batch size', default=128)
    parser.add_argument("-z", "--latentsize", type=int, help='latent size', default=32)
    parser.add_argument("-l", "--learningrate", type=float, help='learning rate', default=1e-4)
    parser.add_argument("--gpu", type=str, help='GPU to use (leave blank for CPU only)', default="")
    parser.add_argument("-g", "--generator", default='dcgan', choices=['dcgan','began'])
    parser.add_argument("-d", "--discriminator", default='dcgan', choices=['none', 'dcgan', 'dcgan-big', 'dcgan-big2'])
    parser.add_argument("--l2_penalty", type=float, help="L2 weight decay term", default=0.0)
    parser.add_argument("--gen_l2p_scale", type=float, help="L2 weight decay scaling term for generator", default=1.0)
    parser.add_argument("--discriminator_lr_scale", type=float, help="LR scaling for the discriminator", default=1)
    parser.add_argument("--dimension", type=int, help='Dimension for the kernel function', default=3)
    parser.add_argument("--epsilon", type=float, help='epsilon', default=1.0)
    parser.add_argument("--threads", type=int, help='number of input threads', default=2)
    parser.add_argument("--dataset", choices=['celebA', 'lsun', 'mnist', 'cifar10'], default='celebA')
    parser.add_argument("--resume_checkpoint", type=str, help='path to model from which to resume', default='')
    parser.add_argument("--checkpoint_every", type=int, help='how often to create a new checkpoint', default=25000)
    parser.add_argument("--logdir", type=str, help='directory for TF logs and summaries', default=default_logdir)
    parser.add_argument("--logdir_suffix", type=str, help='appendix to logdir', default="")
    parser.add_argument("--disc_loss", choices=['l2', 'l1'], default='l2')
    parser.add_argument("--immediate_return", help="only here for debugging purposes", action="store_true")
    parser.add_argument("--sample_images", help="just loads a model and samples images into the given directory, then exists", type=str, default=None)
    parser.add_argument("--sample_images_size", help="How many images to sample", type=int, default=20480)
    parser.add_argument("--remember_previous", help="Reevaluate Discriminator on previous iteration's generator points", action="store_true")
    parser.add_argument("--create_summaries", help="Add a summaries for Tensorboard", action="store_true")
    parser.add_argument("--inception_path", type=str, help='Path to Inception model', default='/publicwork/coulomb_gan')
    parser.add_argument("--fid_stats", type=str, help='Path to statistics for FID (%s will be replaced by the passed dataset)', default='./fid_stats_%s.npz')
    parser.add_argument("--verbosity", help="verbosity level", type=int, default=1)
    return parser


if __name__ == "__main__":
    # by parsing the arguments already, we can bail out now instead of waiting
    # for TF to load, in case the arguments aren't ok
    parser = setup_argumentparser()
    args = parser.parse_args()
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    import numpy as np
    import tensorflow as tf
    from utils import *
    from models import *

    run(args.dataset.lower(), args.generator, args.discriminator, args.latentsize,
        args.dimension, args.epsilon, args.learningrate, args.batch_size, args, args.logdir)
else:
    #os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import numpy as np
    import tensorflow as tf

    from utils import *
    from models import *
