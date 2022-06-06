import numpy as np
from tinygrad.helpers import binary_broadcast
from ..tensor import Function
from ..llops.opencl import GPUBuffer as Buffer
from ..llops.opencl import unary_op, binary_op, reduce_op, perm_axis, inner_slice
from ..llops.opencl import matmul, conv, convdw, convdx

# ************* unary ops *************

class UnaryOp(Function):
  def forward(ctx, input):
    ctx.save_for_backward(input)
    return unary_op(ctx.fop, input, Buffer(input.shape))

  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    return binary_op(ctx.bop, input, grad_output, Buffer(input.shape))

class ReLU(UnaryOp):
  fop = 'max(a, (float)0.)'
  bop = 'b * (a >= 0)'

class Log(UnaryOp):
  fop = 'log(a)'
  bop = 'b / a'

class Exp(UnaryOp):
  fop = 'exp(a)'
  bop = 'b * exp(a)'

# ************* reduce ops *************

def reduce_shape(shape, axis):
  osize = np.array(shape)
  osize[list(axis)] = 1
  return osize

class Sum(Function):
  def forward(ctx, input, axis=None):
    ctx.save_for_backward(input.shape)
    return reduce_op("out += a", input, Buffer(reduce_shape(input.shape, axis)))

  def backward(ctx, grad_output):
    shape_input, = ctx.saved_tensors
    # NOTE: the b Buffer isn't used, since this is just for broadcast
    ret = Buffer(shape_input)
    return binary_op('a', grad_output, ret, ret)

class Max(Function):
  def forward(ctx, input, axis=None):
    ret = reduce_op("out = max(a,out)", input, Buffer(reduce_shape(input.shape, axis)), start="-INFINITY")
    ctx.save_for_backward(input, axis, ret)
    return ret

  def backward(ctx, grad_output):
    input, axis, ret = ctx.saved_tensors
    ret2 = binary_op("1.0*(a==b)", input, ret, Buffer(input.shape))
    div = reduce_op("out += a", ret2, Buffer(reduce_shape(ret2.shape, axis)), start="1e-10")
    binary_op("a/b", ret2, div, ret2)
    return binary_op('a*b', ret2, grad_output, ret2)

# ************* binary ops *************

def unbroadcast(out, in_sh):
  return reduce_op("out += a", out, Buffer(in_sh))

class Add(Function):
  def forward(ctx, x, y):
    ctx.save_for_backward(x.shape, y.shape)
    return binary_op('a+b', x, y, Buffer(binary_broadcast(x.shape, y.shape)))

  def backward(ctx, grad_output):
    shape_x, shape_y = ctx.saved_tensors
    return unbroadcast(grad_output, shape_x) if ctx.needs_input_grad[0] else None, \
           unbroadcast(grad_output, shape_y) if ctx.needs_input_grad[1] else None

class Sub(Function):
  def forward(ctx, x, y):
    ctx.save_for_backward(x.shape, y.shape)
    return binary_op('a-b', x, y, Buffer(binary_broadcast(x.shape, y.shape)))

  def backward(ctx, grad_output):
    shape_x, shape_y = ctx.saved_tensors
    return unbroadcast(grad_output, shape_x) if ctx.needs_input_grad[0] else None, \
           unbroadcast(unary_op('-a', grad_output, Buffer(grad_output.shape)), shape_y) if ctx.needs_input_grad[1] else None

class Mul(Function):
  def forward(ctx, x, y):
    ctx.save_for_backward(x, y)
    return binary_op('a*b', x, y, Buffer(binary_broadcast(x.shape, y.shape)))

  def backward(ctx, grad_output):
    x,y = ctx.saved_tensors
    tmp = Buffer(grad_output.shape)
    grad_x = unbroadcast(binary_op('a*b', y, grad_output, tmp), x.shape) if ctx.needs_input_grad[0] else None
    grad_y = unbroadcast(binary_op('a*b', x, grad_output, tmp), y.shape) if ctx.needs_input_grad[1] else None
    return grad_x, grad_y

class Pow(Function):
  def forward(ctx, x, y):
    ctx.save_for_backward(x, y)
    return binary_op('pow(a,b)', x, y, Buffer(binary_broadcast(x.shape, y.shape)))

  def backward(ctx, grad_output):
    x,y = ctx.saved_tensors
    tmp = Buffer(grad_output.shape)
    grad_x = unbroadcast(binary_op('a*b', grad_output, binary_op('b * pow(a, b-1.0f)', x, y, tmp), tmp), x.shape) if ctx.needs_input_grad[0] else None
    grad_y = unbroadcast(binary_op('a*b', grad_output, binary_op('log(a) * pow(a, b)', x, y, tmp), tmp), y.shape) if ctx.needs_input_grad[1] else None
    return grad_x, grad_y

# ************* movement ops *************

class Reshape(Function):
  def forward(ctx, x, shape):
    ctx.save_for_backward(x.shape)
    shape = tuple(-np.prod(x.shape) // np.prod(shape) if s == -1 else s for s in shape)
    r = Buffer(shape, hostbuf=x)   # NOTE: this is not a copy
    assert np.prod(x.shape) == np.prod(r.shape)
    return r

  def backward(ctx, grad_output):
    in_shape, = ctx.saved_tensors
    return Buffer(in_shape, hostbuf=grad_output)

class Transpose(Function):
  def forward(ctx, x, order=(1,0)):
    ctx.save_for_backward(order)
    ret = Buffer([x.shape[i] for i in order])
    return perm_axis(x, order, ret)

  def backward(ctx, grad_output):
    norder = np.argsort(ctx.order)
    ret = Buffer([grad_output.shape[i] for i in norder])
    return perm_axis(grad_output, norder, ret)

class Slice(Function):
  def forward(ctx, x, arg=None):
    ctx.save_for_backward(x.shape)
    ret = Buffer([y[1]-y[0] for y in arg])
    return inner_slice(x, arg, ret)

  def backward(ctx, grad_output):
    shape, = ctx.saved_tensors
    narg = [(0-p[0], grad_output.shape[i]+(shape[i]-p[1])) for i,p in enumerate(ctx.arg)]
    ret = Buffer([y[1]-y[0] for y in narg])
    return inner_slice(grad_output, narg, ret)

# ************* processing ops *************

class Matmul(Function):
  def forward(ctx, input, weight):
    assert input.shape[-1] == weight.shape[-2]
    ret = Buffer(list(input.shape[0:-1])+[weight.shape[-1]])
    ctx.save_for_backward(input, weight)
    return matmul(input, weight, ret)

  def backward(ctx, grad_output):
    input, weight = ctx.saved_tensors
    grad_input = matmul(grad_output, weight, Buffer(input.shape), transpose_b=True) if ctx.needs_input_grad[0] else None
    grad_weight = matmul(input, grad_output, Buffer(weight.shape), transpose_a=True) if ctx.needs_input_grad[1] else None
    return grad_input, grad_weight

class Conv2D(Function):
  def forward(ctx, x, w, stride=1, groups=1):
    if isinstance(ctx.stride, int): ctx.stride = (ctx.stride, ctx.stride)
    cout,cin,H,W = w.shape
    ys,xs = ctx.stride
    bs,cin_,iy,ix = x.shape
    oy,ox = (iy-(H-ys))//ys, (ix-(W-xs))//xs
    if cin*ctx.groups != cin_: raise Exception(f"Input Tensor shape {x.shape} does not match the shape of the weights {w.shape}. ({cin*ctx.groups} vs. {cin_})")
    assert cout % ctx.groups == 0
    rcout = cout//ctx.groups

    ctx.save_for_backward(x,w)

    # output buffer
    conv_args = H, W, ctx.groups, rcout, cin, oy, ox, iy, ix, ys, xs, bs
    return conv(x, w, Buffer((bs, cout, oy, ox)), conv_args)

  def backward(ctx, grad_output):
    bs,_,oy,ox = grad_output.shape
    x, w = ctx.saved_tensors
    cout,cin,H,W = w.shape
    ys,xs = ctx.stride
    bs,cin_,iy,ix = x.shape
    oy,ox = (iy-(H-ys))//ys, (ix-(W-xs))//xs
    assert cin*ctx.groups == cin_
    assert cout % ctx.groups == 0
    rcout = cout//ctx.groups

    conv_args = H, W, ctx.groups, rcout, cin, oy, ox, iy, ix, ys, xs, bs
    dx = convdx(w, grad_output, Buffer((bs, cin_, iy, ix)), conv_args) if ctx.needs_input_grad[0] else None
    dw = convdw(x, grad_output, Buffer((cout, cin, H, W)), conv_args) if ctx.needs_input_grad[1] else None
    return dx, dw
