from NetworkHiddenLayer import Layer
from Log import log
from cuda_implementation.OneDToTwoDOp import OneDToTwoDOp
from cuda_implementation.CropToBatchImageSizeOp import CropToBatchImageSizeInstance, CropToBatchImageSizeZeroInstance
from cuda_implementation.MultiDirectionalTwoDLSTMOp import MultiDirectionalTwoDLSTMOpInstance
from cuda_implementation.BiDirectionalTwoDLSTMOp import BidirectionalTwoDLSTMOpInstance
from cuda_implementation.CuDNNConvHWBCOp import CuDNNConvHWBCOpValidInstance
from cuda_implementation.PoolHWBCOp import PoolHWBCOp
from cuda_implementation.FractionalMaxPoolingOp import fmp
import theano
import theano.tensor as T
from theano.tensor.nnet import conv
from theano.tensor.signal import downsample
import numpy
from math import sqrt
from ActivationFunctions import strtoact

import theano.printing
from theano.ifelse import ifelse


class TwoDBaseLayer(Layer):
  def __init__(self, n_out, **kwargs):
    kwargs['n_out'] = n_out
    super(TwoDBaseLayer, self).__init__(**kwargs)
    #like in _NoOpLayer
    self.params = {}  # Reset all params.
    self.set_attr('from', ",".join([s.name for s in self.sources]))

  def create_xavier_weights(self, shape, name):
    p = shape[0] + numpy.prod(shape[1:])
    W = numpy.asarray(self.rng.uniform(low=-sqrt(6) / sqrt(p), high = sqrt(6) / sqrt(p), size=shape),
                      dtype=theano.config.floatX)
    return theano.shared(value=W, borrow=True, name=name + "_" + self.name)


class OneDToTwoDLayer(TwoDBaseLayer):
  layer_class = "1Dto2D"
  recurrent = False

  def __init__(self, **kwargs):
    super(OneDToTwoDLayer, self).__init__(1, **kwargs)
    assert len(self.sources) == 2
    n_in = self.sources[0].attrs['n_out']
    n_out = n_in
    sizes = T.cast(self.sources[1].output, "float32")
    assert sizes.ndim == 2
    sizes = sizes.reshape((2, sizes.size / 2)).dimshuffle(1, 0)
    self.output_sizes = sizes
    X = self.sources[0].output
    assert X.ndim == 3
    assert X.dtype == "float32"
    Y = OneDToTwoDOp()(X, sizes)
    if self.attrs['batch_norm']:
      Y = self.batch_norm(Y,n_out,index=sizes)
    self.output = Y
    self.set_attr('n_out', n_out)


class OneDToTwoDFixedSizeLayer(TwoDBaseLayer):
  layer_class = "1Dto2D_fixed_size"
  recurrent = True

  def __init__(self, **kwargs):
    super(OneDToTwoDFixedSizeLayer, self).__init__(1, **kwargs)
    assert len(self.sources) == 1
    X = self.sources[0].output
    assert X.ndim == 3
    assert X.dtype == "float32"

    height = X.shape[2]
    width = T.maximum(T.sum(self.index, axis=0), T.ones_like(self.index[0]))
    batch = X.shape[1]
    sizes = T.zeros((batch, 2), dtype="float32")
    sizes = T.set_subtensor(sizes[:, 0], height)
    sizes = T.set_subtensor(sizes[:, 1], width)
    Y = T.unbroadcast(X.dimshuffle(2, 0, 1, 'x'), 3)
    self.output = Y
    self.output_sizes = sizes
    n_out = 1
    self.set_attr('n_out', n_out)

forget_gate_initial_bias = 1.0
lambda_gate_initial_bias = 0.0

class DeepLSTM(TwoDBaseLayer):
  layer_class = "deep_lstm"
  recurrent = True

  def __init__(self, n_out, depth, **kwargs):
    super(DeepLSTM, self).__init__(n_out, **kwargs)
    X = T.concatenate([s.output for s in self.sources],axis=2).dimshuffle('x',0,1,2).repeat(depth,axis=0)
    n_in = sum([s.attrs['n_out'] for s in self.sources])
    assert X.dtype == "float32"

    width = T.maximum(T.sum(self.index, axis=0), T.ones_like(self.index[0]))
    batch = X.shape[2]
    sizes = T.zeros((batch, 2), dtype="float32")
    sizes = T.set_subtensor(sizes[:, 0], numpy.float32(depth))
    sizes = T.set_subtensor(sizes[:, 1], T.cast(width,'float32'))
    X = T.unbroadcast(X, 0)
    self.output_sizes = sizes

    # dropout
    assert len(self.masks) == 1
    mask = self.masks[0]
    if mask is not None:
      X = self.mass * mask * X

    b1 = self.create_and_add_bias(n_out, "1")
    b2 = self.create_and_add_bias(n_out, "2")

    W1, V_h1, V_v1 = self.create_and_add_2d_lstm_weights(n_in, n_out, "1")
    W2, V_h2, V_v2 = self.create_and_add_2d_lstm_weights(n_in, n_out, "2")

    if str(theano.config.device).startswith('cpu'):
      Y = T.dot(X,W1)[:n_out*2]
    else:
      Y1, Y2 = BidirectionalTwoDLSTMOpInstance(X, W1, W2, V_h1, V_h2, V_v1, V_v2, b1, b2, sizes)[:2]
      Y = T.concatenate([Y1,Y2],axis=3)

    Y.name = 'Y'
    self.set_attr('n_out', n_out*2)
    self.output = Y[-1]

  def create_and_add_2d_lstm_weights(self, n, m, name_suffix):
    W, U, V = self.create_xavier_weights((n, 5 * m), "W" + name_suffix), \
              self.create_xavier_weights((m, 5 * m), "U" + name_suffix), \
              self.create_xavier_weights((m, 5 * m), "V" + name_suffix)
    W = self.add_param(W)
    U = self.add_param(U)
    V = self.add_param(V)
    return W, U, V


  def create_and_add_bias(self, n_cells, name_suffix):
    b_val = numpy.zeros((5 * n_cells,), dtype=theano.config.floatX)
    b_val[1 * n_cells:2 * n_cells] = forget_gate_initial_bias
    b_val[2 * n_cells:3 * n_cells] = lambda_gate_initial_bias
    b = theano.shared(b_val, borrow=True, name="b" + name_suffix + "_" + self.name)
    b = self.add_param(b)
    return b


class TwoDLSTMLayer(TwoDBaseLayer):
  layer_class = "mdlstm"
  recurrent = True

  def __init__(self, n_out, collapse_output=False, directions=4, projection='average', **kwargs):
    super(TwoDLSTMLayer, self).__init__(n_out, **kwargs)
    assert len(self.sources) == 1
    source = self.sources[0]
    n_in = source.attrs['n_out']
    X = source.output
    assert X.ndim == 4
    sizes = source.output_sizes
    self.output_sizes = sizes
    assert directions in [1,2,4], "only 1, 2 or 4 directions are supported"
    assert projection in ['average', 'concat'], "invalid projection"

    #dropout
    assert len(self.masks) == 1
    mask = self.masks[0]
    if mask is not None:
      X = self.mass * mask * X

    b1 = self.create_and_add_bias(n_out, "1")
    b2 = self.create_and_add_bias(n_out, "2")
    if directions >= 1:
      b3 = self.create_and_add_bias(n_out, "3")
      b4 = self.create_and_add_bias(n_out, "4")

    W1, V_h1, V_v1 = self.create_and_add_2d_lstm_weights(n_in, n_out, "1")
    W2, V_h2, V_v2 = self.create_and_add_2d_lstm_weights(n_in, n_out, "2")
    if directions >= 1:
      W3, V_h3, V_v3 = self.create_and_add_2d_lstm_weights(n_in, n_out, "3")
      W4, V_h4, V_v4 = self.create_and_add_2d_lstm_weights(n_in, n_out, "4")

    if str(theano.config.device).startswith('cpu'):
      Y = T.zeros_like(X)
      if projection == 'concat':
        Y = Y.repeat(directions,axis=-1)
        n_out *= directions
    else:
      if directions <= 2:
        Y = BidirectionalTwoDLSTMOpInstance(X, W1, W2, V_h1, V_h2, V_v1, V_v2, b1, b2, sizes)
      else:
        Y = MultiDirectionalTwoDLSTMOpInstance(X, W1, W2, W3, W4, V_h1, V_h2, V_h3, V_h4,
                                                  V_v1, V_v2, V_v3, V_v4, b1, b2, b3, b4, sizes)

      if directions > 1:
        Y = T.stack(Y[:directions],axis=-1)
        if projection == 'average':
          Y = Y.mean(axis=-1)
        elif projection == 'concat':
          Y = Y.reshape((Y.shape[0],Y.shape[1],Y.shape[2],Y.shape[3]*Y.shape[4]))
          n_out *= directions
      else:
        Y = Y[0]

    Y.name = 'Y'
    self.set_attr('n_out', n_out)
    self.set_attr('collapse_output', collapse_output)
    self.set_attr('directions', directions)
    self.set_attr('projection', projection)

    #index handling
    def index_fn(index, size):
      return T.set_subtensor(index[:size], numpy.cast['int8'](1))
    index_init = T.zeros((Y.shape[2],Y.shape[1]), dtype='int8')
    self.index, _ = theano.scan(index_fn, [index_init, T.cast(sizes[:,1],"int32")])
    self.index = self.index.dimshuffle(1, 0)

    if collapse_output == 'sum' or collapse_output == True:
      Y = Y.sum(axis=0)
    elif collapse_output == 'mean':
      Y = Y.mean(axis=0)
    elif collapse_output == 'conv':
      from TheanoUtil import circular_convolution
      Y, _ = theano.scan(lambda x_i,x_p:circular_convolution(x_i,x_p),Y,Y[0])
      Y = Y[-1]
    elif collapse_output == 'flatten':
      self.index = T.ones((Y.shape[0] * Y.shape[1], Y.shape[2]), dtype='int8')
      Y = Y.reshape((Y.shape[0]*Y.shape[1],Y.shape[2],Y.shape[3]))
    elif str(collapse_output).startswith('pad_'):
      pad = numpy.int32(collapse_output.split('_')[-1])
      Y = ifelse(T.lt(Y.shape[0],pad),T.concatenate([Y,T.zeros((pad-Y.shape[0],Y.shape[1],Y.shape[2],Y.shape[3]),'float32')],axis=0),
                 ifelse(T.gt(Y.shape[0],pad),Y[:pad],Y))
      Y = Y.dimshuffle(1,2,3,0).reshape((Y.shape[1],Y.shape[2],Y.shape[3]*Y.shape[0]))
      self.attrs['n_out'] *= pad
    elif collapse_output != False:
      assert False, "invalid collapse mode"

    if self.attrs['batch_norm']:
      Y = self.batch_norm(Y,self.attrs['n_out'],index=sizes if not collapse_output else self.index)

    self.output = Y

  def create_and_add_2d_lstm_weights(self, n, m, name_suffix):
    W, U, V = self.create_xavier_weights((n, 5 * m), "W" + name_suffix), \
              self.create_xavier_weights((m, 5 * m), "U" + name_suffix), \
              self.create_xavier_weights((m, 5 * m), "V" + name_suffix)
    W = self.add_param(W)
    U = self.add_param(U)
    V = self.add_param(V)
    return W, U, V

  def create_and_add_bias(self, n_cells, name_suffix):
    b_val = numpy.zeros((5 * n_cells,), dtype=theano.config.floatX)
    b_val[1 * n_cells:2 * n_cells] = forget_gate_initial_bias
    b_val[2 * n_cells:3 * n_cells] = lambda_gate_initial_bias
    b = theano.shared(b_val, borrow=True, name="b" + name_suffix + "_" + self.name)
    b = self.add_param(b)
    return b


printed_cudnn_warning = False


def conv_crop_pool_op(X, sizes, output_sizes, W, b, n_in, n_maps, filter_height, filter_width, poolsize):
  global printed_cudnn_warning
  import theano.sandbox.cuda as theano_cuda
  have_cudnn = theano_cuda.cuda_enabled and theano.sandbox.cuda.dnn.dnn_available()
  if theano_cuda.cuda_enabled and not have_cudnn and not printed_cudnn_warning:
    print >> log.v1, "warning, cudnn not available, using theano conv implementation"
    printed_cudnn_warning = True

  if have_cudnn:
    conv_op = CuDNNConvHWBCOpValidInstance
    pool_op = PoolHWBCOp(poolsize)
    conv_out = conv_op(X, W, b)
    crop_out = CropToBatchImageSizeInstance(conv_out, sizes)
    Y = pool_op(crop_out)
    Y = CropToBatchImageSizeZeroInstance(Y, output_sizes)
    return Y
  else:
    #note: this solution uses alot of dimshuffles and so also alot of memory
    #I only have this so that I can still run on my laptop for testing
    #it's not really useful for productive use and also not much tested
    filter_shape = (n_maps, n_in, filter_height, filter_width)
    X_shuffled = X.dimshuffle(2, 3, 0, 1)
    conv_out = conv.conv2d(input=X_shuffled, border_mode="valid", filters=W, filter_shape=filter_shape,
                           image_shape=(None, n_in, None, None))
    crop_out = CropToBatchImageSizeInstance(conv_out.dimshuffle(2, 3, 0, 1), sizes).dimshuffle(2, 3, 0, 1)
    if poolsize == (1, 1):
      Y = crop_out
    else:
      #pooling cannot handle width > 512 (only with cuDNN), so we swap the axes and swap them back afterwards
      crop_out = crop_out.dimshuffle(0, 1, 3, 2)
      pooled_out = downsample.max_pool_2d(
        input=crop_out,
        #max_pool_2d wants the sizes in the other order
        ds=poolsize[::-1],
        ignore_border=True
      )
      #unshuffle it
      Y = pooled_out.dimshuffle(0, 1, 3, 2)
    Y = Y.dimshuffle(2, 3, 0, 1)
    Y += b
    return Y


class ConvBaseLayer(TwoDBaseLayer):
  layer_class = "conv_base"
  recurrent = False

  def __init__(self, n_features, filter, activation="tanh", **kwargs):
    kwargs['n_out'] = n_features
    super(ConvBaseLayer, self).__init__(**kwargs)
    assert len(self.sources) == 1
    self.source = self.sources[0]
    self.n_in = self.source.attrs['n_out']
    self.X = self.source.output
    assert self.X.ndim == 4
    self.n_features = n_features

    self.set_attr('n_features', n_features)
    self.set_attr('filter', filter)
    self.set_attr('activation', activation)
    self.set_attr('n_out', n_features)

    #TODO: maybe this ordering is not consistent with Dewis implementation
    self.filter_height = filter[0]
    self.filter_width = filter[1]
    self.activation = strtoact(activation)
    self.W = self.create_conv_weights(n_features, self.n_in, self.filter_height, self.filter_width)
    self.b = self.create_and_add_bias(n_features)

  def create_conv_weights(self, n_features, n_in, filter_height, filter_width, name_suffix = ""):
    filter_shape = (n_features, n_in, filter_height, filter_width)
    W = self.create_xavier_weights(filter_shape, "W" + name_suffix)
    W = self.add_param(W)
    return W

  def create_and_add_bias(self, n_out, name_suffix=""):
    b_val = numpy.zeros((n_out,), dtype=theano.config.floatX)
    b = theano.shared(b_val, borrow=True, name="b" + name_suffix + "_" + self.name)
    b = self.add_param(b)
    return b

  def conv_output_size_from_input_size(self, sizes):
    heights = sizes[:, 0]
    widths = sizes[:, 1]
    heights = heights - self.filter_height + 1
    widths = widths - self.filter_width + 1
    return T.concatenate((heights[:, None], widths[:, None]), axis=1)

printed_pad_warning = False
def maybe_print_pad_warning(_, x):
  global printed_pad_warning
  if x != 0 and not printed_pad_warning:
    print >> log.v2, "Warning, input for conv layer too small, applying padding on the fly, this can cause increased memory usage, longer runtimes and worse results. Consider padding your input data manually. This warning is only printed once, even if the problem occurs multiple times."
    printed_pad_warning = True

class ConvPoolLayer2(ConvBaseLayer):
  layer_class = "conv2"
  recurrent = True

  def __init__(self, pool_size, **kwargs):
    super(ConvPoolLayer2, self).__init__(**kwargs)
    self.pool_size = pool_size
    sizes_raw = self.source.output_sizes

    #handle size problems
    self.output_sizes = self.output_size_from_input_size(sizes_raw)
    size_problem = T.min(self.output_sizes) <= 0
    size_problem = theano.printing.Print(global_fn=maybe_print_pad_warning)(size_problem)
    fixed_sizes = T.maximum(sizes_raw, numpy.array([self.pool_size[0] + self.filter_height - 1, self.pool_size[1] + self.filter_width - 1], dtype="float32"))

    sizes = ifelse(size_problem, fixed_sizes, sizes_raw)
    X_size = T.cast(T.max(sizes, axis=0), "int32")
    def pad_fn(x_t, s):
      x = T.alloc(numpy.cast["float32"](0), X_size[0], X_size[1], self.X.shape[3])
      x = T.set_subtensor(x[:s[0], :s[1]], x_t[:s[0], :s[1]])
      return x
    fixed_X, _ = theano.scan(pad_fn, [self.X.dimshuffle(2,0,1,3), T.cast(sizes_raw, "int32")])
    fixed_X = fixed_X.dimshuffle(1,2,0,3)
    self.X = ifelse(size_problem, T.unbroadcast(fixed_X,3), self.X)
    #end handle size problems

    self.output_sizes = self.output_size_from_input_size(sizes)
    Z = conv_crop_pool_op(self.X, sizes, self.output_sizes, self.W, self.b, self.n_in, self.n_features, self.filter_height,
                          self.filter_width, pool_size)
    Y = self.activation(Z)
    if self.attrs['batch_norm']:
      Y = self.batch_norm(Y,self.attrs['n_out'],index=sizes)

    self.output = Y

    #index handling
    def index_fn(index, size):
      return T.set_subtensor(index[:size], numpy.cast['int8'](1))
    index_init = T.zeros((Y.shape[2],Y.shape[1]), dtype='int8')
    self.index, _ = theano.scan(index_fn, [index_init, T.cast(self.output_sizes[:,1],"int32")])
    self.index = self.index.dimshuffle(1, 0)


  def output_size_from_input_size(self, sizes):
    heights = sizes[:, 0]
    widths = sizes[:, 1]
    heights = heights - self.filter_height + 1
    widths = widths - self.filter_width + 1
    p1, p2 = self.pool_size
    heights //= p1
    widths //= p2
    return T.concatenate((heights[:, None], widths[:, None]), axis=1)


class ConvFMPLayer(ConvBaseLayer):
  layer_class = "conv_fmp"
  recurrent = False

  def __init__(self, factor=numpy.sqrt(2), **kwargs):
    super(ConvFMPLayer, self).__init__(**kwargs)
    conv_out = CuDNNConvHWBCOpValidInstance(self.X, self.W, self.b)
    conv_out_sizes = self.conv_output_size_from_input_size(self.source.output_sizes)
    self.output, self.output_sizes = fmp(conv_out, conv_out_sizes, numpy.cast["float32"](factor))
