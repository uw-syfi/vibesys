# Transpose2D

Transpose2D
In this tutorial, we transpose a tensor along two of its axes using NKI.
In doing so, we learn about:

* The NKI syntax and programming model.

* Multi-dimensional memory address patterns in NKI.

As background, there are two main types of transposition in NKI:

* Transposition between the partition-dimension axis and one of the
free-dimension axes, which is achieved via the
`nki.isa.nc_transpose` instruction.

* Transposition between two axes on the free-dimension, which is achieved
via a `nki.language.copy` instruction, with indexing manipulation
in the free axis to re-arrange the data.

In this example, we’ll focus on the second case: consider a
three-dimensional input tensor `[P, F1, F2]`, where the `P` axis is mapped
to the different SBUF partitions and the `F1` and `F2` axes are
flattened and placed in each partition, with `F1` being the major
dimension. Our goal in this example is to transpose the `F1` and
`F2` axes with a parallel dimension `P`,
to re-arrange the data within each partition. [Figure](#nki-fig-transpose)
below illustrates the input and output tensor layouts.

[![../../../_images/pm-index-2.png](../../../_images/pm-index-2.png)](../../../_images/pm-index-2.png)

Fig. 27 Tensor F1:F2 Transpose

## PyTorch

### Compute kernel


```python
import nki
import nki.isa as nisa
import nki.language as nl


@nki.jit
def tensor_transpose2D_kernel_(in_tensor, shape2D):
  """
  NKI kernel to reorder the elements on axis[1] of the input tensor.

  Every row of the input tensor is a flattened row-major 2D matrix.
  The shape2D argument defines the dimensions of the flattened matrices (#rows,#cols).
  Our goal in this kernel is to transpose these flattened 2D matrices, i.e. make them (#cols,#rows).

  Example:
      in_tensor = [a0,a1,a2,a3,b0,b1,b2,b3,c0,c1,c2,c3]
      shape2D = (3,4)
  this means that in_tensor has 3 rows and 4 columns, i.e. can be represented as:
      [a0,a1,a2,a3]
      [b0,b1,b2,b3]
      [c0,c1,c2,c3]
  after transpose, we expect to get:
      [a0,b0,c0]
      [a1,b1,c1]
      [a2,b2,c2]
      [a3,b3,c3]
  Thus, out_tensor is expected to be [a0,b0,c0,a1,b1,c1,a2,b2,c2,a3,b3,c3]

  Args:
    in_tensor: an input tensor
    shape2D: tuple representing the dimensions to be transposed: (#rows, #cols)
    out_tensor: an output (transposed) tensor
  """
  out_tensor = nl.ndarray(in_tensor.shape, dtype=in_tensor.dtype,
                          buffer=nl.shared_hbm)
  # Gather input shapes
  sz_p, sz_f = in_tensor.shape

  # Allocate tile in on-chip memory and load input data from external memory
  in_tile = nl.ndarray((sz_p, sz_f), dtype=in_tensor.dtype, buffer=nl.sbuf)
  nisa.dma_copy(dst=in_tile, src=in_tensor)

  # Performing f1/f2 transpose
  # ==========================
  # The desired transpose pattern is provided as an input:
  sz_f1, sz_f2 = shape2D

  # We're going to need 3 indices to perform f1:f2 transpose.
  # - i_p0 is the parallel index
  # - i_f1 and i_f2 are both free-dim indices, and will be used to transpose between the f1/f2 axes
  i_p0, i_f1, i_f2 = nl.mgrid[:sz_p, :sz_f1, :sz_f2]

  # Perform the transposition via a SBUF-to-SBUF copy, with access-pattern manipulation
  # Note that we have 2D tensors and 3 indices, since we need to represent a 2D access pattern *per partition*
  # RHS traverses an F1 x F2 matrix in a row major manner
  # LHS traverses an F2 x F1 (new) matrix in a row major manner
  out_tile = nl.ndarray(shape=(sz_p, sz_f2*sz_f1), dtype=out_tensor.dtype)
  out_tile[i_p0, i_f2*sz_f1+i_f1] = nl.copy(in_tile[i_p0, i_f1*sz_f2+i_f2])

  # Finally, we store out_tile to external memory
  nisa.dma_copy(dst=out_tensor, src=out_tile)

  return out_tensor
```


### Launching kernel and testing correctness

To execute the kernel, we prepare tensors `a` and call `tensor_transpose2D_kernel_`:


```python
import torch
from torch_xla.core import xla_model as xm

if __name__ == "__main__":
  device = xm.xla_device()

  P, X, Y = 5, 3, 4
  a = torch.arange(P*X*Y, dtype=torch.int8).reshape((P, X*Y)).to(device=device)
  a_t_nki = torch.zeros((P, Y*X), dtype=torch.int8).to(device=device)

  a_t_nki = tensor_transpose2D_kernel_(a, (X, Y))

  a_t_torch = torch.transpose(a.reshape(P, X, Y), 1, 2).reshape(P, X * Y)

  print(a, a_t_nki, a_t_torch)

  allclose = torch.allclose(a_t_torch, a_t_nki)
  if allclose:
    print("NKI and PyTorch match")
  else:
    print("NKI and PyTorch differ")

  assert allclose
```


## JAX

### Compute kernel

We can reuse the same NKI compute kernel defined for PyTorch above.


```python
import nki
import nki.isa as nisa
import nki.language as nl


@nki.jit
def tensor_transpose2D_kernel_(in_tensor, shape2D):
  """
  NKI kernel to reorder the elements on axis[1] of the input tensor.

  Every row of the input tensor is a flattened row-major 2D matrix.
  The shape2D argument defines the dimensions of the flattened matrices (#rows,#cols).
  Our goal in this kernel is to transpose these flattened 2D matrices, i.e. make them (#cols,#rows).

  Example:
      in_tensor = [a0,a1,a2,a3,b0,b1,b2,b3,c0,c1,c2,c3]
      shape2D = (3,4)
  this means that in_tensor has 3 rows and 4 columns, i.e. can be represented as:
      [a0,a1,a2,a3]
      [b0,b1,b2,b3]
      [c0,c1,c2,c3]
  after transpose, we expect to get:
      [a0,b0,c0]
      [a1,b1,c1]
      [a2,b2,c2]
      [a3,b3,c3]
  Thus, out_tensor is expected to be [a0,b0,c0,a1,b1,c1,a2,b2,c2,a3,b3,c3]

  Args:
    in_tensor: an input tensor
    shape2D: tuple representing the dimensions to be transposed: (#rows, #cols)
    out_tensor: an output (transposed) tensor
  """
  out_tensor = nl.ndarray(in_tensor.shape, dtype=in_tensor.dtype,
                          buffer=nl.shared_hbm)
  # Gather input shapes
  sz_p, sz_f = in_tensor.shape

  # Allocate tile in on-chip memory and load input data from external memory
  in_tile = nl.ndarray((sz_p, sz_f), dtype=in_tensor.dtype, buffer=nl.sbuf)
  nisa.dma_copy(dst=in_tile, src=in_tensor)

  # Performing f1/f2 transpose
  # ==========================
  # The desired transpose pattern is provided as an input:
  sz_f1, sz_f2 = shape2D

  # We're going to need 3 indices to perform f1:f2 transpose.
  # - i_p0 is the parallel index
  # - i_f1 and i_f2 are both free-dim indices, and will be used to transpose between the f1/f2 axes
  i_p0, i_f1, i_f2 = nl.mgrid[:sz_p, :sz_f1, :sz_f2]

  # Perform the transposition via a SBUF-to-SBUF copy, with access-pattern manipulation
  # Note that we have 2D tensors and 3 indices, since we need to represent a 2D access pattern *per partition*
  # RHS traverses an F1 x F2 matrix in a row major manner
  # LHS traverses an F2 x F1 (new) matrix in a row major manner
  out_tile = nl.ndarray(shape=(sz_p, sz_f2*sz_f1), dtype=out_tensor.dtype)
  out_tile[i_p0, i_f2*sz_f1+i_f1] = nl.copy(in_tile[i_p0, i_f1*sz_f2+i_f2])

  # Finally, we store out_tile to external memory
  nisa.dma_copy(dst=out_tensor, src=out_tile)

  return out_tensor
```


### Launching kernel and testing correctness

To execute the kernel, we prepare array `a` and call `tensor_transpose2D_kernel_`:


```python
import jax
import jax.numpy as jnp

if __name__ == "__main__":
  P, X, Y = 5, 37, 44
  a = jax.random.uniform(jax.random.PRNGKey(42), (P, X * Y))
  a_t_nki = tensor_transpose2D_kernel_(a, shape2D=(X, Y))

  a_t_jax = jnp.transpose(a.reshape(P, X, Y), axes=(0, 2, 1)).reshape(P, X * Y)
  print(a, a_t_nki, a_t_jax)

  allclose = jnp.allclose(a_t_jax, a_t_nki)
  if allclose:
    print("NKI and JAX match")
  else:
    print("NKI and JAX differ")

  assert allclose
```


> **Note**
>
> Note
> 
> 
> We pass `shape2D` as kwargs to pass the shape as a compile-time constant
> to the kernel function.

## Download All Source Code

Click the links to download source code of the kernels and the testing code
discussed in this tutorial.

* NKI baremetal implementation: [`transpose2d_nki_kernels.py`](../../downloads/transpose2d_nki_kernels.py)

* 
PyTorch implementation: [`transpose2d_torch.py`](../../downloads/transpose2d_torch.py)

You must also download [`transpose2d_nki_kernels.py`](../../downloads/transpose2d_nki_kernels.py)
into the same folder to run this PyTorch script.

* 
JAX implementation: [`transpose2d_jax.py`](../../downloads/transpose2d_jax.py)

You must also download [`transpose2d_nki_kernels.py`](../../downloads/transpose2d_nki_kernels.py)
into the same folder to run this JAX script.

You can also view the source code in the GitHub repository [nki_samples](https://github.com/aws-neuron/nki-samples/tree/main/src/nki_samples/tutorials/transpose2d/)

### Example usage of the scripts:

Run NKI baremetal implementation:


```python
python3 transpose2d_nki_kernels.py
```


Run PyTorch implementation:


```python
python3 transpose2d_torch.py
```


Run JAX implementation:


```python
python3 transpose2d_jax.py
```