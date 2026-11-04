# AveragePool2D

> **NOTE:** This tutorial contains code examples using deprecated Beta 1 patterns (`nl.mgrid`, `nl.load`, `nl.store`).
> For migration guidance, see the [NKI Migration Guide](../../reference/migration/nki-migration-guide.md) (Beta 1 to Beta 2)
> and the [NKI 0.3.0 Update Guide](../../reference/migration/nki-030-update-guide.md) (Beta 2 to GA).
> Key changes needed: Replace `nl.mgrid` with explicit loops or reshaping, and replace `nl.load`/`nl.store` with `nisa.dma_copy`.

AveragePool2D
In this tutorial, we examine a case of
dimensionality reduction. We implement a 2D AveragePool operation, which
is used in many vision neural networks.
In doing so, we learn about:

* NKI syntax and programming model.

* multi-dimensional memory access patterns in NKI.

The 2D AveragePool operation takes
`C x [H,W]` matrices and reduces each matrix along the `H` and `W`
axes. To leverage free-dimension flexible indexing, we can map the `C`
(parallel) axis to the `P` dimension and `H/W` (contraction)
axes to the `F` dimension.
Performing such a 2D pooling operation requires a 4D memory access
pattern in the `F` dimension, with reduction along two axes.
[Figure](#nki-fig-avgpool)
below illustrates the input and output tensor layouts.

[![../../../_images/pm-index-3.png](../../../_images/pm-index-3.png)](../../../_images/pm-index-3.png)

Fig. 26 2D-Pooling Operation (reducing on axes F2 and F4)

## PyTorch

### Compute kernel


```python
import nki
import nki.language as nl
from neuronxcc.nki.typing import tensor

@nki.jit
def tensor_avgpool_kernel(in_tensor, pool_size):
  """NKI kernel to compute a 2D avg-pool operation

  Args:
      in_tensor: an input tensor, of shape C x H x W
      pool_size: an integer representing a (square) pool-window size

  Return:
      out_tensor: the resulting output tensor, of shape C x (H/pool_size) x (W/pool_size)
  """

  # Get input/output dimensions
  sz_cin, sz_hin, sz_win = in_tensor.shape
  sz_hout = sz_hin // pool_size
  sz_wout = sz_win // pool_size
  # Create output tensor shared between all SPMD instances as result tensor
  out_tensor = nl.ndarray((sz_cin, sz_hout, sz_wout), dtype=in_tensor.dtype,
                          buffer=nl.shared_hbm)

  # Set relevant sizes
  sz_p = sz_cin
  sz_pool = pool_size

  # Generate pool index patterns (requires two extra dimensions, for the pool window)
  i0, i1, i2, i3, i4 = nl.mgrid[0:sz_p, 0:sz_hin//sz_pool, 0:sz_pool, 0:sz_win//sz_pool, 0:sz_pool]

  # Load input data from external memory to on-chip memory
  in_tile = nl.ndarray((sz_p, sz_hin, sz_win), dtype=in_tensor.dtype, buffer=nl.sbuf)
  nisa.dma_copy(dst=in_tile, src=in_tensor)

  # Perform the pooling operation:
  # We use numpy's advanced indexing, in order to extend in_tile to 5D, and then reduce-average two dimension.
  # axis[0] is the index for p_dim, and thus doesn't participate in the reduction operation.
  # axis[1] and axis[2] together index the rows, with axis[2] responsible for inner strides
  # (i.e. inside a pooling window), and axis[1] responsible for the outer strides. As such, we reduce over axis[2].
  # Similarly, axis[3] and axis[4] together index the columns, and we thus reduce over axis[4].
  out_tile : tensor[sz_p, sz_hout, sz_wout] = nl.sum(in_tile[i0, sz_pool*i1+i2, sz_pool*i3+i4],
                                                     axis=[2,4]) / (pool_size*pool_size)

  # Store the results back to hbm
  nl.store(out_tensor, value=out_tile)

  # Transfer the ownership of `out_tensor` to the caller
  return out_tensor
```


### Launching kernel and testing correctness

To execute the kernel, we prepare tensors `in_tensor` and call `tensor_avgpool_kernel`:


```python
import torch
from torch_xla.core import xla_model as xm

if __name__ == "__main__":
  device = xm.xla_device()

  # Now let's run the kernel
  POOL_SIZE = 2
  C, HIN, WIN = 2, 6, 6
  HOUT, WOUT = HIN//POOL_SIZE, WIN//POOL_SIZE

  in_tensor = torch.arange(C * HIN * WIN, dtype=torch.bfloat16).reshape(C, HIN, WIN).to(device=device)
  out_nki = torch.zeros((C, HOUT, WOUT), dtype=torch.bfloat16).to(device=device)

  out_nki = tensor_avgpool_kernel(in_tensor, POOL_SIZE)

  out_torch = torch.nn.functional.avg_pool2d(in_tensor, POOL_SIZE, POOL_SIZE)

  print(in_tensor, out_nki, out_torch) # an implicit XLA barrier/mark-step

  if (out_nki == out_torch).all():
    print("NKI and Torch match")
  else:
    print("NKI and Torch differ")
```


## JAX

### Compute kernel

Let’s reuse the same NKI kernel implementation defined for PyTorch above:


```python
import nki
import nki.language as nl
from neuronxcc.nki.typing import tensor

@nki.jit
def tensor_avgpool_kernel(in_tensor, pool_size):
  """NKI kernel to compute a 2D avg-pool operation

  Args:
      in_tensor: an input tensor, of shape C x H x W
      pool_size: an integer representing a (square) pool-window size

  Return:
      out_tensor: the resulting output tensor, of shape C x (H/pool_size) x (W/pool_size)
  """

  # Get input/output dimensions
  sz_cin, sz_hin, sz_win = in_tensor.shape
  sz_hout = sz_hin // pool_size
  sz_wout = sz_win // pool_size
  # Create output tensor shared between all SPMD instances as result tensor
  out_tensor = nl.ndarray((sz_cin, sz_hout, sz_wout), dtype=in_tensor.dtype,
                          buffer=nl.shared_hbm)

  # Set relevant sizes
  sz_p = sz_cin
  sz_pool = pool_size

  # Generate pool index patterns (requires two extra dimensions, for the pool window)
  i0, i1, i2, i3, i4 = nl.mgrid[0:sz_p, 0:sz_hin//sz_pool, 0:sz_pool, 0:sz_win//sz_pool, 0:sz_pool]

  # Load input data from external memory to on-chip memory
  in_tile = nl.ndarray((sz_p, sz_hin, sz_win), dtype=in_tensor.dtype, buffer=nl.sbuf)
  nisa.dma_copy(dst=in_tile, src=in_tensor)

  # Perform the pooling operation:
  # We use numpy's advanced indexing, in order to extend in_tile to 5D, and then reduce-average two dimension.
  # axis[0] is the index for p_dim, and thus doesn't participate in the reduction operation.
  # axis[1] and axis[2] together index the rows, with axis[2] responsible for inner strides
  # (i.e. inside a pooling window), and axis[1] responsible for the outer strides. As such, we reduce over axis[2].
  # Similarly, axis[3] and axis[4] together index the columns, and we thus reduce over axis[4].
  out_tile : tensor[sz_p, sz_hout, sz_wout] = nl.sum(in_tile[i0, sz_pool*i1+i2, sz_pool*i3+i4],
                                                     axis=[2,4]) / (pool_size*pool_size)

  # Store the results back to hbm
  nl.store(out_tensor, value=out_tile)

  # Transfer the ownership of `out_tensor` to the caller
  return out_tensor
```


In order to pass `pool_size` as a compile time constant, we pass `pool_size` as kwargs.


```python
out_nki = tensor_avgpool_kernel(in_array, pool_size=POOL_SIZE)
```


We write a reference JAX implementation of `AveragePool2D` as JAX does
not have a primitive for it.


```python
import jax.numpy as jnp

# Reference JAX implementation
def jax_average_pool_2D(in_tensor, pool_size):
  c, h_in, w_in = in_tensor.shape
  reshaped = in_tensor.reshape(c, h_in // pool_size, pool_size, w_in // pool_size, pool_size)
  return jnp.nanmean(reshaped, axis=(2, 4))
```


### Launching kernel and testing correctness

To execute the kernel, we prepare array `in_array` and invoke the kernel caller function `tensor_avgpool_kernel`:


```python
if __name__ == "__main__":
  POOL_SIZE = 2
  C, HIN, WIN = 2, 6, 6
  HOUT, WOUT = HIN//POOL_SIZE, WIN//POOL_SIZE

  in_array = jnp.arange(C * HIN * WIN, dtype=jnp.float32).reshape(C, HIN, WIN)

  out_nki = tensor_avgpool_kernel(in_array, pool_size=POOL_SIZE)
  out_jax = jax_average_pool_2D(in_array, pool_size=POOL_SIZE)

  print(in_array, out_nki, out_jax)

  if jnp.allclose(out_nki, out_jax):
    print("NKI and JAX match")
  else:
    print("NKI and JAX differ")
```


## Download All Source Code

Click the links to download source code of the kernels and the testing code
discussed in this tutorial.

* NKI baremetal implementation: [`average_pool2d_nki_kernels.py`](../../downloads/average_pool2d_nki_kernels.py)

* 
PyTorch implementation: [`average_pool2d_torch.py`](../../downloads/average_pool2d_torch.py)

You must also download [`average_pool2d_nki_kernels.py`](../../downloads/average_pool2d_nki_kernels.py)
into the same folder to run this PyTorch script.

* 
JAX implementation: [`average_pool2d_jax.py`](../../downloads/average_pool2d_jax.py)

You must also download [`average_pool2d_nki_kernels.py`](../../downloads/average_pool2d_nki_kernels.py)
into the same folder to run this JAX script.

You can also view the source code in the GitHub repository [nki_samples](https://github.com/aws-neuron/nki-samples/tree/main/src/nki_samples/tutorials/average_pool2d/)

### Example usage of the scripts:

Run NKI baremetal implementation:


```python
python3 average_pool2d_nki_kernels.py
```


Run PyTorch implementation:


```python
python3 average_pool2d_torch.py
```


Run JAX implementation:


```python
python3 average_pool2d_jax.py
```