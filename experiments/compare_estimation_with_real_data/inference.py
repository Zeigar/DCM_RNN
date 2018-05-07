import sys
# global setting, you need to modify it accordingly
if '/Users/yuanwang' in sys.executable:
    PROJECT_DIR = '/Users/yuanwang/Google_Drive/projects/Gits/DCM_RNN'
    print("It seems a local run on Yuan's laptop")
    print("PROJECT_DIR is set as: " + PROJECT_DIR)
    import matplotlib

    sys.path.append('dcm_rnn')
elif '/share/apps/python3/' in sys.executable:
    PROJECT_DIR = '/home/yw1225/projects/DCM_RNN'
    print("It seems a remote run on NYU HPC")
    print("PROJECT_DIR is set as: " + PROJECT_DIR)
    import matplotlib
    matplotlib.use('agg')
else:
    PROJECT_DIR = '.'
    print("Not sure executing machine. Make sure to set PROJECT_DIR properly.")
    print("PROJECT_DIR is set as: " + PROJECT_DIR)
    import matplotlib
import matplotlib.pyplot as plt
import tensorflow as tf
import tf_model as tfm
import toolboxes as tb
import numpy as np
import os
import pickle
import datetime
import warnings
import sys
import random
import training_manager
import multiprocessing
from multiprocessing.pool import Pool
import itertools
import copy
import pandas as pd
from scipy.interpolate import interp1d
import importlib
from scipy.fftpack import idct, dct
import math as mth
import scipy.io as sio
import time

def get_target_curve(y_rnn, confounds, y_observation):
    n_time_point = y_observation.shape[0]
    n_region = y_observation.shape[1]
    n_confounds = confounds.shape[1]
    residues = np.zeros((n_time_point, n_region))
    beta = np.zeros((n_confounds, n_region))
    for i in range(n_region):
        '''
        a = np.concatenate((y_rnn[:, i].reshape((n_time_point, 1)), confounds), axis=1)
        temp = np.linalg.lstsq(a, y_observation[:, i])[0]
        residue[:, i] = y_observation[:, i] - np.matmul(a[:, 1:], temp[1:])
        '''
        temp = np.linalg.lstsq(confounds, y_observation[:, i] - y_rnn[:, i])[0]
        residues[:, i] = y_observation[:, i] - np.matmul(confounds, temp)
        beta[:, i] = temp
    return [residues, beta]


EXPERIMENT_PATH = os.path.join(PROJECT_DIR, 'experiments', 'compare_estimation_with_real_data')
DATA_PATH = os.path.join(EXPERIMENT_PATH, 'data')
RAW_DATA_PATH = os.path.join(DATA_PATH, 'dcm_rnn_initial.mat')
# TEMPLATE_PATH = os.path.join(PROJECT_DIR, 'experiments', 'compare_estimation_with_simulated_data', 'data', 'du_DCM_RNN.pkl')
SAVE_PATH = os.path.join(EXPERIMENT_PATH, 'results', 'estimation_dcm_rnn.pkl')
SAVE_EXTENDED_PATH = os.path.join(EXPERIMENT_PATH, 'results', 'estimation_dcm_rnn_extended.pkl')
SAVE_PATH_FREE_ENERGY = os.path.join(EXPERIMENT_PATH, 'results', 'free_energy_rnn.mat')


MAX_EPOCHS = 32 * 4
CHECK_STEPS = 1
N_RECURRENT_STEP = 256
DATA_SHIFT = 2
MAX_BACK_TRACK = 4
MAX_CHANGE = 0.005
KEEP_RATE_IN_DROP_OUT = 0.75
TARGET_T_DELTA = 1. / 16
BATCH_SIZE = 128
N_CONFOUNDS = 19

trainable_flags = trainable_flags = {'Wxx': True,
                       'Wxxu': True,
                       'Wxu': True,
                       'alpha': False,
                       'E0': False,
                       'k': True,
                       'gamma': False,
                       'tao': True,
                       'epsilon': True,
                       'V0': False,
                       'TE': False,
                       'r0': False,
                       'theta0': False,
                       'x_h_coupling': False
                       }

timer = {}

# load data
spm_data = sio.loadmat(RAW_DATA_PATH)
spm_data['stimulus_names'] = ['Photic', 'Motion', 'Attention']
spm_data['node_names'] = ['V1', 'V5', 'SPC']
spm_data['u'] = spm_data['u'].todense()
du_hat = tb.DataUnit()

# process spm_data,
# up sample u and y to 16 frame/second
shape = list(spm_data['u'].shape)
n_time_point = shape[0]
spm_data['u_upsampled'] = du_hat.resample(spm_data['u'], shape, order=3)
spm_data['y_upsampled'] = du_hat.resample(spm_data['y'], shape, order=3)
# scale the signal to proper range
spm_data['y_upsampled'] = spm_data['y_upsampled']
y_observation = spm_data['y_upsampled']
n_segments = mth.ceil((len(y_observation) - N_RECURRENT_STEP) / DATA_SHIFT)

# assume the observation model is
# y = DCM_RNN(u) + Confounds * weights + noise
# Confounds are the first cosine transfer basis (19)
confounds = idct(np.eye(n_time_point)[:, :N_CONFOUNDS], axis=0, norm='ortho')
# plt.plot(confounds)

# settings
n_region = 3
n_stimuli = 3

# connectivity parameters ABC initials
A = -np.eye(n_region) * 1
B = [np.zeros((n_region, n_region))] * n_region
C = np.zeros((n_region, n_stimuli))
C[0, 0] = 0.
x_parameter_initial = {'A': A, 'B': B, 'C': C}

# in DCM-RNN, the corresponding connectivity parameters are Wxx, Wxxu, Wxu
x_parameter_initial_in_graph = du_hat.calculate_dcm_rnn_x_matrices(x_parameter_initial['A'],
                                                               x_parameter_initial['B'],
                                                               x_parameter_initial['C'],
                                                               TARGET_T_DELTA)

# hemodynamic parameter initials
h_parameter_initial = du_hat.get_standard_hemodynamic_parameters(n_region)

# mask for the supported parameter (1 indicates the parameter is tunable)
mask = {'Wxx': np.ones((n_region, n_region)),
        'Wxxu': [np.zeros((n_region, n_region)) for _ in range(n_stimuli)],
        'Wxu': np.zeros((n_region, n_stimuli))}
mask['Wxx'][0, 2] = 0
mask['Wxx'][2, 0] = 0
# mask['Wxx'][0, 1] = 0
# mask['Wxx'][1, 0] = 0
# mask['Wxx'][1, 2] = 0
mask['Wxxu'][1][1, 0] = 1
mask['Wxxu'][2][1, 2] = 1
mask['Wxu'][0, 0] = 1


# set up weights for each loss terms
prior_weight = (16) * (N_RECURRENT_STEP / y_observation.shape[0]) * (BATCH_SIZE)
loss_weights_tensorflow = {
    'y': 1.,
    'q': 1.,
    'prior_x': prior_weight,
    'prior_h': prior_weight,
    'prior_hyper': prior_weight
}
loss_weights_du = {
    'y': 1.,
    'q': 1.,
    'prior_x': 16,
    'prior_h': 16,
    'prior_hyper': 16
}


# set up DataUnit for estimation
du_hat._secured_data['n_node'] = n_region
du_hat._secured_data['n_stimuli'] = n_stimuli
du_hat._secured_data['t_delta'] = TARGET_T_DELTA
du_hat._secured_data['u'] = spm_data['u_upsampled']
du_hat._secured_data['t_scan'] = n_time_point * TARGET_T_DELTA
du_hat._secured_data['A'] = x_parameter_initial['A']
du_hat._secured_data['B'] = x_parameter_initial['B']
du_hat._secured_data['C'] = x_parameter_initial['C']
du_hat._secured_data['hemodynamic_parameter'] = h_parameter_initial
du_hat._secured_data['initial_x_state'] = du_hat.set_initial_neural_state_as_zeros(n_region)
du_hat._secured_data['initial_h_state'] = du_hat.set_initial_hemodynamic_state_as_inactivated(n_region)
du_hat._secured_data['x_nonlinearity_type'] = 'None'
du_hat._secured_data['u_type'] = 'carbox'
du_hat.loss_weights = loss_weights_du
du_hat.complete_data_unit(start_category=2, if_check_property=False)



# build dcm_rnn model
# importlib.reload(tfm)
tf.reset_default_graph()
timer['build_model'] = time.time()
dr = tfm.DcmRnn()
dr.n_region = n_region
dr.n_stimuli = n_stimuli
dr.n_time_point = du_hat.get('n_time_point')
dr.t_delta = TARGET_T_DELTA
dr.n_recurrent_step = N_RECURRENT_STEP
dr.max_back_track_steps = MAX_BACK_TRACK
dr.max_parameter_change_per_iteration = MAX_CHANGE
dr.trainable_flags = trainable_flags
dr.loss_weights = loss_weights_tensorflow
dr.batch_size = BATCH_SIZE
dr.build_main_graph_parallel(neural_parameter_initial=x_parameter_initial,
                             hemodynamic_parameter_initial=h_parameter_initial)

# process after building the main graph
dr.support_masks = dr.setup_support_mask(mask)
dr.x_parameter_nodes = [v for v in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                                     scope=dr.variable_scope_name_x_parameter)]
dr.trainable_variables_nodes = [v for v in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)]
dr.trainable_variables_names = [v.name for v in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)]
du_hat.variable_names_in_graph = du_hat.parse_variable_names(dr.trainable_variables_names)

# prepare data for training
y_target = get_target_curve(du_hat.get('y'), confounds, y_observation)[0]
data = {
    'u': tb.split(spm_data['u_upsampled'], n_segment=dr.n_recurrent_step, n_step=DATA_SHIFT)}
data_hat = {
    'x_initial': tb.split(du_hat.get('x'), n_segment=dr.n_recurrent_step, n_step=DATA_SHIFT, shift=0),
    'h_initial': tb.split(du_hat.get('h'), n_segment=dr.n_recurrent_step, n_step=DATA_SHIFT, shift=1),
    'y': tb.split(y_target, n_segment=dr.n_recurrent_step, n_step=DATA_SHIFT, shift=dr.shift_u_y)
}
for k in data_hat.keys():
    data_hat[k] = data_hat[k][: n_segments]
for k in data.keys():
    data[k] = data[k][: n_segments]
batches = tb.make_batches(data['u'], data_hat['x_initial'], data_hat['h_initial'], data_hat['y'],
                          batch_size=dr.batch_size, if_shuffle=True)

# start session
print('start session')
timer['start_session'] = time.time()
isess = tf.InteractiveSession()
isess.run(tf.global_variables_initializer())
dr.update_variables_in_graph(isess, dr.x_parameter_nodes,
                             [x_parameter_initial_in_graph['Wxx']]
                             + x_parameter_initial_in_graph['Wxxu']
                             + [x_parameter_initial_in_graph['Wxu']])

isess.run(tf.assign(dr.loss_y_weight, loss_weights_tensorflow['y']))
isess.run(tf.assign(dr.loss_q_weight, loss_weights_tensorflow['q']))
isess.run(tf.assign(dr.loss_prior_x_weight, loss_weights_tensorflow['prior_x']))
isess.run(tf.assign(dr.loss_prior_h_weight, loss_weights_tensorflow['prior_h']))
isess.run(tf.assign(dr.loss_prior_hyper_weight, loss_weights_tensorflow['prior_hyper']))
dr.max_parameter_change_per_iteration = MAX_CHANGE

print('start inference')
loss_differences = []
step_sizes = []
for epoch in range(MAX_EPOCHS):
    print('')
    print('epoch:    ' + str(epoch))
    timer['epoch_' + str(epoch)] = time.time()
    gradients = []

    # bar = progressbar.ProgressBar(max_value=len(batches))
    for batch in batches:
        grads_and_vars = \
            isess.run(dr.grads_and_vars,
                      feed_dict={
                          dr.u_placeholder: batch['u'],
                          dr.x_state_initial: batch['x_initial'],
                          dr.h_state_initial: batch['h_initial'],
                          dr.y_true: batch['y']
                      })
        # collect gradients and logs
        gradients.append(grads_and_vars)

    # updating with back-tracking
    ## collect statistics before updating
    step_size = 0
    y_noise_index = du_hat.variable_names_in_graph.index('y_noise')
    y_noise = grads_and_vars[y_noise_index][1] - grads_and_vars[y_noise_index][0] * step_size
    loss_original = du_hat.calculate_loss(y_target, y_noise)

    ## sum gradients
    grads_and_vars = gradients[-1]
    for idx, grad_and_var in enumerate(grads_and_vars):
        grads_and_vars[idx] = (sum([gv[idx][0] for gv in gradients]), grads_and_vars[idx][1])

    ## apply mask to gradients
    variable_names = [v.name for v in tf.trainable_variables()]
    for idx in range(len(grads_and_vars)):
        variable_name = variable_names[idx]
        grads_and_vars[idx] = (grads_and_vars[idx][0] * dr.support_masks[variable_name], grads_and_vars[idx][1])

    # adaptive step size, making max value change dr.max_parameter_change_per_iteration
    max_gradient = max([np.max(np.abs(np.array(g[0])).flatten()) for g in grads_and_vars])
    STEP_SIZE = dr.max_parameter_change_per_iteration / max_gradient
    print('max gradient:    ' + str(max_gradient))
    print('STEP_SIZE:    ' + str(STEP_SIZE))

    # try to find proper step size
    step_size = STEP_SIZE
    count = 0
    du_hat.update_trainable_variables(grads_and_vars, step_size)

    Wxx = du_hat.get('Wxx')
    stable_flag = du_hat.check_transition_matrix(Wxx, 1.)
    while not stable_flag:
        count += 1
        if count == dr.max_back_track_steps:
            step_size = 0.
            dr.decrease_max_parameter_change_per_iteration()
        else:
            step_size = step_size / 2
        warnings.warn('not stable')
        print('step_size=' + str(step_size))
        du_hat.update_trainable_variables(grads_and_vars, step_size)
        Wxx = du_hat.get('Wxx')
        stable_flag = du_hat.check_transition_matrix(Wxx, 1.)

    try:
        du_hat.regenerate_data()
        y_noise_index = du_hat.variable_names_in_graph.index('y_noise')
        y_noise = grads_and_vars[y_noise_index][1] - grads_and_vars[y_noise_index][0] * step_size
        loss_updated = du_hat.calculate_loss(y_target, y_noise)
    except:
        loss_updated['total'] = float('inf')

    while loss_updated['total'] > loss_original['total'] or np.isnan(loss_updated['total']):
        count += 1
        if count == dr.max_back_track_steps:
            step_size = 0.
            dr.decrease_max_parameter_change_per_iteration()
        else:
            step_size = step_size / 2
        print('step_size=' + str(step_size))
        try:
            du_hat.update_trainable_variables(grads_and_vars, step_size)
            du_hat.regenerate_data()
            y_noise_index = du_hat.variable_names_in_graph.index('y_noise')
            y_noise = grads_and_vars[y_noise_index][1] - grads_and_vars[y_noise_index][0] * step_size
            loss_updated = du_hat.calculate_loss(y_target, y_noise)
        except:
            pass
        if step_size == 0.0:
            break

    # calculate the optimal noise variance
    y_noise = du_hat.solve_for_noise_lambda(y_target, y_noise)
    parameters_updated = [-val[0] * step_size + val[1] for val in grads_and_vars]
    y_noise_index = du_hat.variable_names_in_graph.index('y_noise')
    parameters_updated[y_noise_index] = y_noise

    # update parameters in graph with found step_size
    if step_size > 0:
        dr.update_variables_in_graph(isess, dr.trainable_variables_nodes, parameters_updated)

    loss_differences.append(loss_original['total'] - loss_updated['total'])
    step_sizes.append(step_size)

    # regenerate connector data and target y
    data_hat['x_initial'] = tb.split(du_hat.get('x'), n_segment=dr.n_recurrent_step, n_step=DATA_SHIFT, shift=0)
    data_hat['h_initial'] = tb.split(du_hat.get('h'), n_segment=dr.n_recurrent_step, n_step=DATA_SHIFT, shift=1)

    # regenerate target y
    y_target, beta = get_target_curve(du_hat.get('y'), confounds, y_observation)
    data_hat['y'] = tb.split(y_target, n_segment=dr.n_recurrent_step, n_step=DATA_SHIFT, shift=dr.shift_u_y)
    for k in data_hat.keys():
        data_hat[k] = data_hat[k][: n_segments]


    # make batches and drop out
    batches = tb.make_batches(data['u'], data_hat['x_initial'],
                              data_hat['h_initial'], data_hat['y'],
                              batch_size=dr.batch_size,
                              if_shuffle=False)
    # use guided drop out, the probability of a segment to get dropped is proportional to the error in the segment
    # batches = tb.random_drop(batches, ration=KEEP_RATE_IN_DROP_OUT)
    y_error = du_hat.get('y') - y_target
    y_error_segments = tb.split(y_error, n_segment=dr.n_recurrent_step, n_step=DATA_SHIFT, shift=dr.shift_u_y)
    error_batches = tb.make_batches(data['u'], data_hat['x_initial'],
                              data_hat['h_initial'], y_error_segments,
                              batch_size=dr.batch_size,
                              if_shuffle=False)


    # y_error_sse = np.sum(np.square(y_error), 1)
    p_sampling = np.array([np.linalg.norm(s['y']) for s in error_batches])
    # smooth it to make it less sensitive to noise (outlier)
    # coefficient = dct(p_sampling)
    # coefficient[12:] = 0
    # p_sampling = idct(coefficient)
    p_sampling = p_sampling / np.sum(p_sampling)
    # sampling
    segment_indexes = np.random.choice(len(p_sampling), int(len(p_sampling) * KEEP_RATE_IN_DROP_OUT),
                                       replace=False, p=p_sampling)
    batches = [batches[i] for i in segment_indexes]


    '''
     plt.plot(p_sampling)
        y_reproduced = np.zeros(y_observation.shape)
        for i in range(dr.n_region):
            plt.subplot(dr.n_region, 1, i + 1)
            x_axis = du_hat.get('t_delta') * np.array(range(0, n_time_point))
            # a = np.concatenate((du_hat.get('y')[:, i].reshape((n_time_point, 1)), confounds), axis=1)
            temp = np.linalg.lstsq(confounds, y_observation[:, i] - du_hat.get('y')[:, i])[0]
            y_reproduced[:, i] = np.matmul(confounds, temp) + du_hat.get('y')[:, i]
            plt.plot(x_axis, y_observation[:, i])
            plt.plot(x_axis, y_reproduced[:, i], '--')

            temp = np.zeros(y_reproduced[:, i].shape)
            temp[0: len(p_sampling)] = p_sampling
            plt.plot(x_axis, temp * 5)
    '''

    report = pd.DataFrame(index=['original', 'updated', 'reduced', 'reduced %'],
                          columns=['loss_total', 'loss_y', 'loss_q', 'loss_prior_x', 'loss_prior_h',
                                   'loss_prior_hyper'])
    for name_long in report.keys():
        name = name_long[5:]
        report[name_long] = [loss_original[name], loss_updated[name],
                             loss_original[name] - loss_updated[name],
                             (loss_original[name] - loss_updated[name]) / loss_original[name]]

    print('applied step size:    ' + str(step_size))
    print(report)

    print(du_hat.get('Wxx'))
    print(du_hat.get('Wxxu'))
    print(du_hat.get('Wxu'))
    print(du_hat.get('hemodynamic_parameter')[['k', 'tao', 'epsilon']])
    print('y_noise: ' + str(y_noise))
    print('beta: ')
    print(beta[:4, :])
    du_hat.y_true = y_observation
    du_hat.beta = beta
    du_hat.timer = timer
    du_hat.y_noise = y_noise
    pickle.dump(du_hat, open(SAVE_PATH, 'wb'))
    # save data for free energy calculation
    dcm_rnn_free_energy = {}
    dcm_rnn_free_energy['A'] = du_hat.get('A')
    dcm_rnn_free_energy['B'] = du_hat.get('B')
    dcm_rnn_free_energy['C'] = du_hat.get('C')
    dcm_rnn_free_energy['y'] = du_hat.get('y')
    dcm_rnn_free_energy['transit'] = np.log(np.array(du_hat.get('hemodynamic_parameter')['tao']) / 2.)
    dcm_rnn_free_energy['decay'] = np.log(np.array(du_hat.get('hemodynamic_parameter')['k']) / 0.64)
    dcm_rnn_free_energy['epsilon'] = np.log(np.array(du_hat.get('hemodynamic_parameter')['epsilon']) / 1.)
    dcm_rnn_free_energy['Ce'] = np.array(np.exp(-y_noise))
    dcm_rnn_free_energy['beta'] = beta
    sio.savemat(SAVE_PATH_FREE_ENERGY, mdict={'dcm_rnn_free_energy': dcm_rnn_free_energy})

timer['end'] = time.time()
print('optimization finished.')

y_reproduced = np.zeros(y_observation.shape)
for i in range(dr.n_region):
    plt.subplot(dr.n_region, 1, i + 1)
    x_axis = du_hat.get('t_delta') * np.array(range(0, n_time_point))
    # a = np.concatenate((du_hat.get('y')[:, i].reshape((n_time_point, 1)), confounds), axis=1)
    temp = np.linalg.lstsq(confounds, y_observation[:, i] - du_hat.get('y')[:, i])[0]
    y_reproduced[:, i] = np.matmul(confounds, temp) + du_hat.get('y')[:, i]
    plt.plot(x_axis, y_observation[:, i])
    plt.plot(x_axis, y_reproduced[:, i], '--')



du_hat.y_true = y_observation
du_hat.beta = beta
du_hat.timer = timer
du_hat.y_noise = y_noise
pickle.dump(du_hat, open(SAVE_PATH, 'wb'))

# save extended results
du_hat.extended_data = {}
du_hat.extended_data['u_upsampled'] = spm_data['u_upsampled']
du_hat.extended_data['y_upsampled'] = y_observation
du_hat.extended_data['y_reproduced'] = y_reproduced
du_hat.extended_data['confounds'] = confounds
du_hat.extended_data['y_noise'] = y_noise
du_hat.extended_data['beta'] = beta
pickle.dump(du_hat, open(SAVE_EXTENDED_PATH, 'wb'))





