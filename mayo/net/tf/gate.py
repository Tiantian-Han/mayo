import math

import numpy as np
import tensorflow as tf
from tensorflow.contrib import slim

from mayo.util import Percent, memoize_method


class GateError(Exception):
    """Gating-related exceptions.  """


class GatePolicyError(GateError):
    """Unrecognized policy.  """


class GateParameterValueError(GateError):
    """Incorrect parameters used.  """


class GateGranularityTypeError(GateError):
    """Incorrect granularity used.  """


def _check_policy(policy):
    _accepted_policies = ['naive', 'parametric_gamma']
    if policy in _accepted_policies:
        return policy
    raise GatePolicyError(
        'Unrecognized policy {}, we accept one of {}.'
        .format(policy, ', '.join(_accepted_policies)))


def _subsample(constructor, tensor, granularity, pool, policy, scope):
    num, height, width, channels = tensor.shape
    if granularity == 'channel':
        kernel = [height, width]
    elif granularity == 'vector':
        kernel = [1, width]
    else:
        raise GateGranularityTypeError(
            'Unrecognized granularity {!r}.'.format(granularity))
    # pool
    pool_params = {
        'padding': 'VALID',
        'kernel_size': kernel,
        'stride': 1,
        'scope': scope,
    }
    # max pool is hardware-friendlier
    if pool == 'max':
        subsampled = constructor.instantiate_max_pool(
            None, tensor, pool_params)
    elif pool == 'l2':
        # FIXME this cannot do vector-wise
        subsampled = tf.nn.l2_loss(tensor)
        # tensor = tf.square(tensor)
        # subsampled = constructor.instantiate_average_pool(
        #     None, tensor, pool_params)
    elif pool in ('l1', 'avg'):
        if pool == 'l1':
            tensor = tf.abs(tensor)
        subsampled = constructor.instantiate_average_pool(
            None, tensor, pool_params)
    else:
        raise GateParameterValueError(
            'feature extract type not supported.')
    num, height, width, channels = subsampled.shape
    if granularity == 'channel' and not (height == width == 1):
        raise GateParameterValueError(
            'We expect subsampled image for channel granularity to be 1x1.')
    if granularity == 'vector' and width != 1:
        raise GateParameterValueError(
            'We expect subsampled width for vector granularity to be 1.')
    return tf.stop_gradient(subsampled)


def _gate_network(
        constructor, tensor, granularity, pool, policy,
        kernel_size, stride, padding, num_outputs,
        normalizer_fn, normalizer_params, activation_fn, scope):
    subsampled = _subsample(
        constructor, tensor, granularity, pool, policy, scope)
    if granularity == 'channel':
        kernel = 1
    elif granularity == 'vector':
        if isinstance(kernel_size, int):
            kernel_height = kernel_size
        else:
            kernel_height, _ = kernel_size
        kernel = [kernel_height, 1]
        if not isinstance(padding, str):
            if isinstance(padding, int):
                padding_height = padding
            else:
                padding_height, _ = padding
            padding = [padding, 0]
        if isinstance(stride, int):
            stride_height = stride
        else:
            stride_height, _ = stride
        stride = [stride_height, 1]
    else:
        raise GateGranularityTypeError(
            'Unrecognized granularity {!r}.'.format(granularity))
    normalizer_params = dict(normalizer_params, **{
        'is_training': constructor.is_training,
        'scope': None,  # use default scope
    })
    params = {
        'kernel_size': kernel,
        'stride': stride,
        'padding': padding,
        'num_outputs': num_outputs,
        'biases_initializer': tf.constant_initializer(1.0),
        'weights_initializer': tf.truncated_normal_initializer(stddev=0.01),
        'normalizer_fn': normalizer_fn,
        'normalizer_params': normalizer_params,
        'activation_fn': activation_fn,
        'scope': scope,
    }
    padded = constructor.instantiate_numeric_padding(None, subsampled, params)
    return constructor.instantiate_convolution(None, padded, params)


def _descriminate_by_density(tensor, density, granularity):
    """
    Mark a portion of top elements in tensor to true, where the portion is
    approximately the specified density.

    tensor (tf.Tensor): the input tensor.
    density (float): the percentage of elements to mark as true.
    granularity (str):
        The target granularity, can either be `channel` or `height`.
    """
    if not (0 < density <= 1):
        raise GateParameterValueError(
            'Gate density value {} is out of range (0, 1].'.format(density))
    # not training with the output as we train the predictor `gate`
    tensor = tf.stop_gradient(tensor)
    # number of active elemetns
    num, height, width, channels = tensor.shape
    if granularity == 'channel':
        num_elements = channels
    elif granularity == 'vector':
        num_elements = width * channels
    else:
        raise GateGranularityTypeError(
            'Unrecognized granularity {!r}.'.format(granularity))
    num_active = math.ceil(int(num_elements) * density)
    # reshape the last dimensions into one
    reshaped = tf.reshape(tensor, [num, -1])
    # top_k, where k is the number of active channels
    top, _ = tf.nn.top_k(reshaped, k=(num_active + 1))
    # disable channels with smaller activations
    threshold = tf.reduce_min(top, axis=[1], keep_dims=True)
    active = tf.reshape(reshaped > threshold, tensor.shape)
    return tf.stop_gradient(active)


def _regularized_gate(
        constructor, node, conv_input, conv_output,
        kernel_size, stride, padding, density, granularity, pool, policy,
        weight, normalizer_fn, normalizer_params, activation_fn, scope):
    """
    Regularize gate by making gate output to predict whether subsampled
    conv output is in top-`density` elements as close as possible.

    node (mayo.net.graph.LayerNode): The convolution layer node.
    conv_input (tf.Tensor): The input of the convolution layer.
    conv_output (tf.Tensor): The output from convolution layer.
    kernel_size (tuple or int): The size of the convolutional kernel.
    stride (int): The stride size.
    padding (str or int): The zero padding.
    density (float): The target density.
    granularity (str):
        The target granularity, can either be `channel` or `height`.
    pool (str):
        The preferred feature extraction method, can be `max`, `l1`, `l2`,
        or `avg`.
    policy (str): The policy used.
    weight (float): The weight of the gate regularizer loss.
    normalizer_fn (func): The normalizer function.
    normalizer_params (dict): The parameters used to call normalizer_fn.
    activation_fn: The activation function used.
    scope (str): The scope name.

    Returns regularized gate output.
    """
    # gating network
    num_outputs = int(conv_output.shape[-1])
    gate_scope = '{}/gate'.format(scope)
    gate_output = _gate_network(
        constructor, conv_input, granularity, pool, policy,
        kernel_size, stride, padding, num_outputs,
        normalizer_fn, normalizer_params, activation_fn, gate_scope)
    loss = None
    loss_name = tf.GraphKeys.REGULARIZATION_LOSSES
    if policy == 'naive':
        # output subsample
        subsample_scope = '{}/subsample'.format(scope)
        subsampled = _subsample(
            constructor, conv_output, granularity, pool, policy,
            subsample_scope)
        # training
        # policy descriminator: we simply match max values in each channel
        # using a loss regularizer
        if weight > 0:
            loss = tf.losses.mean_squared_error(
                subsampled, gate_output, weights=weight,
                loss_collection=loss_name)
    elif policy == 'parametric_gamma':
        if weight > 0:
            loss = weight * tf.nn.l2_loss(gate_output)
            tf.add_to_collection(loss_name, loss)
    else:
        raise GatePolicyError
    if loss is not None:
        constructor.session.estimator.register(loss, 'gate.loss', node)
    return gate_output


class GateLayers(object):
    """Layer implementations for gated convolution.  """

    def _register_gate(self, node, gate, active):
        history = None if self.is_training else 'infinite'
        self.session.estimator.register(
            gate, 'gate.output', node, history=history)
        if active is not None:
            self.session.estimator.register(
                active, 'gate.active', node, history=history)

    @staticmethod
    def _gate_loss_formatter(estimator):
        # gating loss for printing
        losses = estimator.get_histories('gate.loss')
        total_losses = None
        for loss_history in losses.values():
            if total_losses is None:
                total_losses = list(loss_history)
            else:
                total_losses = [
                    a + b for a, b in zip(total_losses, loss_history)]
        if total_losses is None:
            loss_mean = 0
        else:
            loss_mean = np.mean(total_losses)
        if loss_mean > 0:
            loss_std = Percent(np.std(total_losses) / loss_mean)
        else:
            loss_std = '?%'
        return 'gate.loss: {:.5f}±{}'.format(loss_mean, loss_std)

    @staticmethod
    def _gate_density_formatter(estimator):
        gates = estimator.get_values('gate.active')
        valid = total = 0
        for layer, gate in gates.items():
            valid += np.sum(gate.astype(np.float32) != 0)
            total += gate.size
        return 'gate: {}'.format(Percent(valid / total))

    @memoize_method
    def _register_gate_formatters(self):
        self.session.estimator.register_formatter(self._gate_loss_formatter)
        self.session.estimator.register_formatter(self._gate_density_formatter)

    def instantiate_gated_convolution(self, node, tensor, params):
        density = params.pop('density')
        granularity = params.pop('granularity', 'channel')
        pool = params.pop('pool', 'max')
        policy = _check_policy(params.pop('policy', 'parametric_gamma'))
        weight = params.pop('weight', 0.01)
        should_gate = params.pop('should_gate', True)
        kernel_size = params['kernel_size']
        stride = params.get('stride', 1)
        padding = params.get('padding', 'SAME')

        # delay normalization
        normalizer_fn = params.pop('normalizer_fn', None)
        normalizer_params = params.pop('normalizer_params', None)
        if normalizer_fn:
            # disable bias
            params['biases_initializer'] = None

        # delay activation
        activation_fn = params.get('activation_fn', tf.nn.relu)
        params['activation_fn'] = None

        # convolution
        output = self.instantiate_convolution(None, tensor, params)

        # predictor gate network
        gate = _regularized_gate(
            self, node, tensor, output, kernel_size, stride, padding,
            density, granularity, pool, policy, weight,
            normalizer_fn, normalizer_params, activation_fn, params['scope'])

        if policy == 'parametric_gamma':
            if normalizer_fn is not slim.batch_norm:
                raise GatePolicyError(
                    'Policy "{}" is used, we expect slim.batch_norm to '
                    'be used but it is absent in {}.'.format(policy, node))

        # normalization
        if normalizer_fn:
            if policy == 'parametric_gamma':
                normalizer_params = dict(normalizer_params, **{
                    'scale': False,
                    'center': False,
                    'activation_fn': None,
                    'scope': '{}/BatchNorm'.format(params['scope']),
                    'is_training': self.is_training,
                })
                output = self.instantiate_batch_normalization(
                    None, output, normalizer_params)
                beta_scope = '{}/gate/shift'.format(params['scope'])
                beta = tf.get_variable(
                    beta_scope, shape=output.shape[-1], dtype=tf.float32,
                    initializer=tf.constant_initializer(0.1), trainable=True)
                # gate output is the parametric gamma value
                output = gate * output + beta
            else:
                output = normalizer_fn(output, **normalizer_params)

        # activation
        if activation_fn is not None:
            output = activation_fn(output)

        # gating
        if should_gate:
            active = _descriminate_by_density(gate, density, granularity)
            output *= tf.cast(active, tf.float32)
            # register gate sparsity for printing
            self._register_gate_formatters()
        else:
            active = None
        self._register_gate(node, gate, active)
        return output
