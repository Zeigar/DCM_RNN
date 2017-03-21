import tensorflow as tf
import DCM_RNN.tf_model as tfm
import DCM_RNN.toolboxes as tb
import numpy as np
import os
import matplotlib.pyplot as plt
# import importlib
import copy
import pickle
import datetime

# importlib.reload(tfm)
# importlib.reload(tb)

# global setting
MAX_EPOCHS = 50
CHECK_STEPS = 1
STOP_THRESHOLD = 1e-3
IF_SHOW_FMRI_SIGNAL = False
IF_SAVE_IN_TRAIN = False

# load in data
current_dir = os.getcwd()
print('working directory is ' + current_dir)
if current_dir.split('/')[-1] == "DCM-RNN":
    os.chdir(current_dir + '/experiments/infer_x_from_y')
data_path = "../../DCM_RNN/resources/template0.pkl"
du = tb.load_template(data_path)

# build model
dr = tfm.DcmRnn()
dr.collect_parameters(du)
# for initializer graph, hemodynamic parameters are set non-trainable
for key in dr.trainable_flags_h.keys():
    dr.trainable_flags_h[key] = False
dr.build_an_initializer_graph(hemodynamic_parameter_initial=None)

# prepare data
data = {}
data['x_true'], data['y_true'] = tb.split_data_for_initializer_graph(
    du.get('x'), du.get('y'), n_segment=dr.n_recurrent_step, n_step=dr.shift_data, shift_x_y=dr.shift_x_y)
data['x_hat'] = tb.split(0.25 * np.ones(du.get('x').shape) * np.mean(du.get('y'), axis=0),
                         n_segment=dr.n_recurrent_step, n_step=dr.shift_data)

# take partial data for test
n_segments = 8
n_time_point_testing = dr.n_recurrent_step + (n_segments - 1) * dr.shift_data
data['x_true'] = data['x_true'][:n_segments]
data['y_true'] = data['y_true'][:n_segments]
data['x_hat'] = data['x_hat'][:n_segments]

# training
x_hat_previous = copy.deepcopy(data['x_hat'])
with tf.Session() as sess:
    sess.run(tf.global_variables_initializer())

    # show initial states
    '''
    epoch = 0
    sess.run(dr.clear_loss_total)
    h_initial_segment = dr.set_initial_hemodynamic_state_as_inactivated(n_node=dr.n_region).astype(np.float32)
    for idx in range(n_segments):
        # assign proper values
        sess.run(dr.assign_x_state_stacked, feed_dict={dr.x_state_stacked_placeholder: data['x_hat'][idx]})
        sess.run(tf.assign(dr.h_state_initial, h_initial_segment))
        loss_total, h_initial_segment = sess.run(
            [dr.sum_loss, dr.h_connector],
            feed_dict={dr.y_true: data['y_true'][idx]})
    summary = sess.run(dr.merged_summary)
    dr.summary_writer.add_summary(summary, epoch)

    x_hat_temp = tb.merge(data['x_hat'], n_segment=dr.n_recurrent_step, n_step=dr.shift_data)
    x_true_temp = du.get('x')[:n_time_point_testing, :]
    plt.figure()
    plt.plot(x_hat_temp)
    plt.plot(x_true_temp)
    print("Epoch:", '%04d' % epoch, "y_total_loss=", "{:.9f}".format(loss_total))
    print("Epoch:", '%04d' % epoch, "x_total_loss=", "{:.9f}".format(tb.mse(x_hat_temp, x_true_temp)))
    '''

    # Fit all training data
    for epoch in range(MAX_EPOCHS):
        sess.run(tf.global_variables_initializer())
        sess.run(dr.clear_loss_total)
        h_initial_segment = dr.set_initial_hemodynamic_state_as_inactivated(n_node=dr.n_region).astype(np.float32)
        for idx in range(n_segments):
            # assign proper data
            sess.run(dr.assign_x_state_stacked, feed_dict={dr.x_state_stacked_placeholder: data['x_hat'][idx]})
            sess.run(tf.assign(dr.h_state_initial, h_initial_segment))
            # training
            sess.run(dr.train, feed_dict={dr.y_true: data['y_true'][idx]})
            # collect results
            data['x_hat'][idx], h_initial_segment = sess.run([dr.x_state_stacked, dr.h_connector])

        # merge and re-split estimated x
        x_hat_temp = tb.merge(data['x_hat'], n_segment=dr.n_recurrent_step, n_step=dr.shift_data)
        data['x_hat'] = tb.split(x_hat_temp, n_segment=dr.n_recurrent_step, n_step=dr.shift_data)

        # Display logs per epoch step
        # loss_total = 0
        if epoch % CHECK_STEPS == 0:
            sess.run(dr.clear_loss_total)
            h_initial_segment = dr.set_initial_hemodynamic_state_as_inactivated(n_node=dr.n_region).astype(np.float32)
            for idx in range(n_segments):
                # assign proper value
                sess.run(dr.assign_x_state_stacked, feed_dict={dr.x_state_stacked_placeholder: data['x_hat'][idx]})
                sess.run(tf.assign(dr.h_state_initial, h_initial_segment))
                # calculate loss and prepare for next segment
                loss_total, h_initial_segment = sess.run(
                    [dr.sum_loss, dr.h_connector],
                    feed_dict={dr.y_true: data['y_true'][idx]})
            summary = sess.run(dr.merged_summary)
            dr.summary_writer.add_summary(summary, epoch + 1)
            x_hat_temp = tb.merge(data['x_hat'], n_segment=dr.n_recurrent_step, n_step=dr.shift_data)
            x_true_temp = du.get('x')[:n_time_point_testing, :]
            print("Epoch:", '%04d' % epoch, "y_total_loss=", "{:.9f}".format(loss_total))
            print("Epoch:", '%04d' % epoch, "x_total_loss=", "{:.9f}".format(tb.mse(x_hat_temp, x_true_temp)))

            # save result
            if IF_SAVE_IN_TRAIN:
                x_hat = tb.merge(data['x_hat'], n_segment=dr.n_recurrent_step, n_step=dr.shift_data)
                x_true = tb.merge(data['x_true'], n_segment=dr.n_recurrent_step, n_step=dr.shift_data)

                data_saved = {}
                data_saved['MAX_EPOCHS'] = MAX_EPOCHS
                data_saved['CHECK_STEPS'] = CHECK_STEPS
                data_saved['STOP_THRESHOLD'] = STOP_THRESHOLD
                #data_saved['du'] = du
                #data_saved['dr'] = dr
                data_saved['epoch'] = epoch

                data_saved['n_segments'] = n_segments
                data_saved['x_hat'] = x_hat
                data_saved['x_true'] = x_true
                #data_saved['y_hat'] = y_hat
                #data_saved['y_true'] = y_true

                dt = datetime.datetime.now()
                file_name = "%04d%02d%02d%02d%02d%02d.pkl" % (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
                pickle.dump(data_saved, open(file_name, "wb"))

            # check stop criterion
            relative_change = tb.rmse(np.concatenate(x_hat_previous), np.concatenate(data['x_hat']))
            if relative_change < STOP_THRESHOLD:
                print('Relative change: ' + str(relative_change))
                print('Stop criterion met, stop training')
                break
            else:
                x_hat_previous = copy.deepcopy(data['x_hat'])

    print("Optimization Finished!")
    x_hat = tb.merge(data['x_hat'], n_segment=dr.n_recurrent_step, n_step=dr.shift_data)
    x_true = du.get('x')[:n_time_point_testing, :]
    plt.figure()
    plt.plot(x_true)
    plt.plot(x_hat, '--')

# show predicted fMRI signal
h_initial_segment = dr.set_initial_hemodynamic_state_as_inactivated(n_node=dr.n_region).astype(np.float32)

with tf.Session() as sess:
    sess.run(tf.global_variables_initializer())
    y_hat = dr.run_initializer_graph(sess, h_initial_segment, data['x_hat'][: n_segments])[0]
    y_true = dr.run_initializer_graph(sess, h_initial_segment, data['x_true'][: n_segments])[0]

y_hat = tb.merge(y_hat, n_segment=dr.n_recurrent_step, n_step=dr.shift_data)
y_true = tb.merge(y_true, n_segment=dr.n_recurrent_step, n_step=dr.shift_data)

if IF_SHOW_FMRI_SIGNAL:
    plt.figure()
    plt.subplot(1, 2, 1)
    plt.plot(y_true)
    # plt.title('y_true')
    plt.subplot(1, 2, 1)
    plt.plot(y_hat, '--')
    plt.title('y_true and y_hat')
    plt.subplot(1, 2, 2)
    plt.plot(y_true - y_hat)
    plt.title('error, rmse=' + str(tb.rmse(y_true, y_hat)))

# save result
data_saved = {}
data_saved['MAX_EPOCHS'] = MAX_EPOCHS
data_saved['CHECK_STEPS'] = CHECK_STEPS
data_saved['STOP_THRESHOLD'] = STOP_THRESHOLD
# data_saved['du'] = du
# data_saved['dr'] = dr
data_saved['epoch'] = epoch

data_saved['n_segments'] = n_segments
data_saved['x_hat'] = x_hat
data_saved['x_true'] = x_true
data_saved['y_hat'] = y_hat
data_saved['y_true'] = y_true

dt = datetime.datetime.now()
file_name = "%04d%02d%02d%02d%02d%02d.pkl" % (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
pickle.dump(data_saved, open(file_name, "wb"))
