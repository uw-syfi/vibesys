# Single Program, Multiple Data (SPMD) Tensor Addition

Single Program, Multiple Data (SPMD) Tensor Addition
In this tutorial we write a simple tensor addition kernel using NKI in PyTorch and JAX. In
doing so, we learn about:

* The NKI syntax and [Logical Neuron Cores (LNC)](../lnc.md#nki-about-lnc).

* Best practices for validating and benchmarking your custom kernel
against a reference native PyTorch or JAX implementation.

## PyTorch

### Compute kernel

We start by defining the compute kernel that has large tensor inputs,
but operates on a subset of the tensor at a tile size of `[128, 512]`.
The partition dimension tile size is chosen according to the tile size
restrictions (nki.language.tile_size.pmax),
while the free dimension tile size is chosen arbitrarily (`512`).


```python
import nki
import nki.isa as nisa
import nki.language as nl


@nki.jit
def nki_tensor_add_kernel_(a_input, b_input):
  """NKI kernel to compute element-wise addition of two input tensors

  This kernel assumes strict input/output sizes can be uniformly tiled to [128,512]

  Args:
      a_input: a first input tensor
      b_input: a second input tensor

  Returns:
      c_output: an output tensor
  """
  # Create output tensor shared between all SPMD instances as result tensor
  c_output = nl.ndarray(a_input.shape, dtype=a_input.dtype, buffer=nl.shared_hbm)

  # Calculate tile offsets based on current 'program'
  offset_i_x = nl.program_id(0) * 128
  offset_i_y = nl.program_id(1) * 512

  # Allocate tiles in on-chip memory (SBUF)
  a_tile = nl.ndarray((128, 512), dtype=a_input.dtype, buffer=nl.sbuf)
  b_tile = nl.ndarray((128, 512), dtype=b_input.dtype, buffer=nl.sbuf)
  c_tile = nl.ndarray((128, 512), dtype=a_input.dtype, buffer=nl.sbuf)

  # Load input data from device memory (HBM) to on-chip memory (SBUF)
  nisa.dma_copy(dst=a_tile, src=a_input[offset_i_x:offset_i_x+128, offset_i_y:offset_i_y+512])
  nisa.dma_copy(dst=b_tile, src=b_input[offset_i_x:offset_i_x+128, offset_i_y:offset_i_y+512])

  # compute a + b
  nisa.tensor_tensor(dst=c_tile, op=nl.add, data1=a_tile, data2=b_tile)

  # store the addition results back to device memory (c_output)
  nisa.dma_copy(dst=c_output[offset_i_x:offset_i_x+128, offset_i_y:offset_i_y+512], src=c_tile)

  # Transfer the ownership of `c_output` to the caller
  return c_output
```


In this example:

* We define the NKI kernel in `nki_tensor_add_kernel_`, decorate it with the
nki.jit decorator to call the nki compiler to compile the kernel.

* Inside, we first allocate tensor `c_output` as the result of the kernel

* Next, we define offsets into the tensors, based on the ID of
the worker executing the code (`nl.program_id`). We allocate tiles
in on-chip memory (SBUF) using `nl.ndarray` and use direct slicing
to load data. See NKI Programming Model for more information on
different tensor indexing modes.

* We use `nl.program_id` to enable SPMD execution (single-program,
multiple-data, see [Logical Neuron Cores (LNC)](../lnc.md#nki-about-lnc)),
where each worker only operates on a (sub-tensor) tile of the
input/output tensors. By accessing its own `program_id`, each
worker can calculate the offsets it needs to access the correct
tiles.

* The first axis of the tensor (mapped to the partition-dimension) is
tiled into blocks of 128, based on hardware restrictions (see [Tile
Size Considerations](../tiling-overview.md#nki-tile-size)).
The second axis (mapped to the free-dimension) is tiled into blocks of 512 (no tile-size constraint,
since the addition operation is performed on the Vector engine, the only restriction is on-chip memory capacity).

* We then load sub-tensors data from tensors `a_input` and
`b_input` using `nisa.dma_copy`, to place the tiles `a_tile` and
`b_tile` in the on-chip memory (SBUF)

* We sum them using `nisa.tensor_tensor` to compute `c_tile`, and store it back to DRAM in the
relevant portion of the `c_output` tensor, using `nisa.dma_copy`.
Since both inputs and output are the same shape, we can use the same
set of indices to access all three tensors.

* At the end, we use `return` statement to transfer the ownership of
tensor `c_output` to the caller of the kernel.

### SPMD execution

We declare a helper function, to launch the compute-kernel with appropriate
grid/block sizes, to perform the computation over the whole input tensors.


```python
def nki_tensor_add(a_input, b_input):
  """NKI kernel caller to compute element-wise addition of two input tensors

  This kernel caller lifts tile-size restriction, by applying the kernel on tiles of the inputs/outputs

  Args:
      a_input: a first input tensor, of shape [N*128, M*512]
      b_input: a second input tensor, of shape [N*128, M*512]

  Returns:
      a tensor of shape [N*128, M*512], the result of a_input + b_input
  """

  # The SPMD launch grid denotes the number of kernel instances.
  # In this case, we use a 2D grid where the size of each invocation is 128x512
  grid_x = a_input.shape[0] // 128
  grid_y = a_input.shape[1] // 512

  return nki_tensor_add_kernel_[grid_x, grid_y](a_input, b_input)
```


We are using a two-dimensional grid, where the first dimension of the
tensor is tiled in the X dimension of the grid, while the second
dimension is tiled in the Y dimension of the grid. In this scenario we
assume that tensor sizes are a multiple of maximum tile sizes allowed,
so we do not need to handle partial tiles.

### Launching kernel and testing correctness

To execute the kernel, we prepare tensors `a` and `b`, and call the
`nki_tensor_add` helper function. We also verify the correctness of the NKI kernel against, torch by
comparing the outputs of both, using `torch.allclose`:


```python
import torch
from torch_xla.core import xla_model as xm

if __name__ == "__main__":
  device = xm.xla_device()

  a = torch.rand((256, 1024), dtype=torch.bfloat16).to(device=device)
  b = torch.rand((256, 1024), dtype=torch.bfloat16).to(device=device)

  output_nki = nki_tensor_add(a, b)
  print(f"output_nki={output_nki}")

  output_torch = a + b
  print(f"output_torch={output_torch}")

  allclose = torch.allclose(output_torch, output_nki, atol=1e-4, rtol=1e-2)
  if allclose:
    print("NKI and Torch match")
  else:
    print("NKI and Torch differ")

  assert allclose
```


Output:


```python
2023-12-29 15:18:00.000558:  14283  INFO ||NEURON_CACHE||: Compile cache path: /var/tmp/neuron-compile-cache
2023-12-29 15:18:00.000559:  14283  INFO ||NEURON_CC_WRAPPER||: Call compiler with cmd: ['neuronx-cc', '--target=trn1', 'compile', '--framework', 'XLA', '/tmp/neuroncc_compile_workdir/49f554a2-2c55-4a88-8054-cc9f20824a46/model.MODULE_5007921933048625946+d41d8cd9.hlo.pb', '--output', '/tmp/neuroncc_compile_workdir/49f554a2-2c55-4a88-8054-cc9f20824a46/model.MODULE_5007921933048625946+d41d8cd9.neff', '--verbose=35']
.
Compiler status PASS
output_nki=tensor([[0.9297, 0.8359, 1.1719,  ..., 0.4648, 0.2188, 0.9336],
        [0.3906, 1.3125, 0.8789,  ..., 1.6562, 1.7734, 0.9531],
        [0.6445, 1.1406, 1.3281,  ..., 0.9531, 0.8711, 0.9336],
        ...,
        [0.4023, 0.6406, 1.5312,  ..., 0.7617, 0.7734, 0.3359],
        [0.8125, 0.7422, 1.2109,  ..., 0.8516, 1.2031, 0.5430],
        [1.3281, 1.2812, 1.3984,  ..., 1.2344, 0.8711, 0.5664]],
       device='xla:1', dtype=torch.bfloat16)
2023-12-29 15:18:02.000219:  14463  INFO ||NEURON_CACHE||: Compile cache path: /var/tmp/neuron-compile-cache
2023-12-29 15:18:02.000220:  14463  INFO ||NEURON_CC_WRAPPER||: Call compiler with cmd: ['neuronx-cc', '--target=trn1', 'compile', '--framework', 'XLA', '/tmp/neuroncc_compile_workdir/2e135b73-1c3b-45e4-a6f0-2c4b105c20e5/model.MODULE_10032327759287407517+d41d8cd9.hlo.pb', '--output', '/tmp/neuroncc_compile_workdir/2e135b73-1c3b-45e4-a6f0-2c4b105c20e5/model.MODULE_10032327759287407517+d41d8cd9.neff', '--verbose=35']
.
Compiler status PASS
output_torch=tensor([[0.9297, 0.8359, 1.1719,  ..., 0.4648, 0.2188, 0.9336],
        [0.3906, 1.3125, 0.8789,  ..., 1.6562, 1.7734, 0.9531],
        [0.6445, 1.1406, 1.3281,  ..., 0.9531, 0.8711, 0.9336],
        ...,
        [0.4023, 0.6406, 1.5312,  ..., 0.7617, 0.7734, 0.3359],
        [0.8125, 0.7422, 1.2109,  ..., 0.8516, 1.2031, 0.5430],
        [1.3281, 1.2812, 1.3984,  ..., 1.2344, 0.8711, 0.5664]],
       device='xla:1', dtype=torch.bfloat16)
2023-12-29 15:18:03.000797:  14647  INFO ||NEURON_CACHE||: Compile cache path: /var/tmp/neuron-compile-cache
2023-12-29 15:18:03.000798:  14647  INFO ||NEURON_CC_WRAPPER||: Call compiler with cmd: ['neuronx-cc', '--target=trn1', 'compile', '--framework', 'XLA', '/tmp/neuroncc_compile_workdir/74f8b6ae-76d9-4dd8-af7f-e5e1c40a27a3/model.MODULE_5906037506311912405+d41d8cd9.hlo.pb', '--output', '/tmp/neuroncc_compile_workdir/74f8b6ae-76d9-4dd8-af7f-e5e1c40a27a3/model.MODULE_5906037506311912405+d41d8cd9.neff', '--verbose=35']
.
Compiler status PASS
NKI and Torch match
```


Note that the tensor values you see will differ from what’s printed
above, since this example uses torch.rand to initialize the inputs.

## JAX

### Compute kernel

We can reuse the same NKI compute kernel defined for PyTorch above.


```python
import nki
import nki.isa as nisa
import nki.language as nl


@nki.jit
def nki_tensor_add_kernel_(a_input, b_input):
  """NKI kernel to compute element-wise addition of two input tensors

  This kernel assumes strict input/output sizes can be uniformly tiled to [128,512]

  Args:
      a_input: a first input tensor
      b_input: a second input tensor

  Returns:
      c_output: an output tensor
  """
  # Create output tensor shared between all SPMD instances as result tensor
  c_output = nl.ndarray(a_input.shape, dtype=a_input.dtype, buffer=nl.shared_hbm)

  # Calculate tile offsets based on current 'program'
  offset_i_x = nl.program_id(0) * 128
  offset_i_y = nl.program_id(1) * 512

  # Allocate tiles in on-chip memory (SBUF)
  a_tile = nl.ndarray((128, 512), dtype=a_input.dtype, buffer=nl.sbuf)
  b_tile = nl.ndarray((128, 512), dtype=b_input.dtype, buffer=nl.sbuf)
  c_tile = nl.ndarray((128, 512), dtype=a_input.dtype, buffer=nl.sbuf)

  # Load input data from device memory (HBM) to on-chip memory (SBUF)
  nisa.dma_copy(dst=a_tile, src=a_input[offset_i_x:offset_i_x+128, offset_i_y:offset_i_y+512])
  nisa.dma_copy(dst=b_tile, src=b_input[offset_i_x:offset_i_x+128, offset_i_y:offset_i_y+512])

  # compute a + b
  nisa.tensor_tensor(dst=c_tile, op=nl.add, data1=a_tile, data2=b_tile)

  # store the addition results back to device memory (c_output)
  nisa.dma_copy(dst=c_output[offset_i_x:offset_i_x+128, offset_i_y:offset_i_y+512], src=c_tile)

  # Transfer the ownership of `c_output` to the caller
  return c_output
```


### SPMD execution

Now we can also declare a helper function, to launch the compute-kernel with
appropriate grid/block sizes, to perform the computation:


```python
def nki_tensor_add(a_input, b_input):
  """NKI kernel caller to compute element-wise addition of two input tensors

  This kernel caller lifts tile-size restriction, by applying the kernel on tiles of the inputs/outputs

  Args:
      a_input: a first input tensor, of shape [N*128, M*512]
      b_input: a second input tensor, of shape [N*128, M*512]

  Returns:
      a tensor of shape [N*128, M*512], the result of a_input + b_input
  """

  # The SPMD launch grid denotes the number of kernel instances.
  # In this case, we use a 2D grid where the size of each invocation is 128x512
  grid_x = a_input.shape[0] // 128
  grid_y = a_input.shape[1] // 512

  return nki_tensor_add_kernel_[grid_x, grid_y](a_input, b_input)
```


We are using a two-dimensional grid, where the first dimension of the
tensor is tiled in the X dimension of the grid, while the second
dimension is tiled in the Y dimension of the grid. In this scenario we
assume that tensor sizes are a multiple of maximum tile sizes allowed,
so we do not need to handle partial tiles.

### Launching kernel and testing correctness

To execute the kernel, we prepare arrays `a` and `b`, and call the
`nki_tensor_add` helper function. We also verify the correctness of the NKI kernel against, JAX by
comparing the outputs of both, using `jax.numpy.allclose`:


```python
import jax
import jax.numpy as jnp

if __name__ == "__main__":

  seed_a, seed_b = jax.random.split(jax.random.PRNGKey(42))
  a = jax.random.uniform(seed_a, (256, 1024), dtype=jnp.bfloat16)
  b = jax.random.uniform(seed_b, (256, 1024), dtype=jnp.bfloat16)

  output_nki = nki_tensor_add(a, b)
  print(f"output_nki={output_nki}")

  output_jax = a + b
  print(f"output_jax={output_jax}")

  allclose = jnp.allclose(output_jax, output_nki, atol=1e-4, rtol=1e-2)
  if allclose:
    print("NKI and JAX match")
  else:
    print("NKI and JAX differ")

  assert allclose
```


Output:


```python
.
Compiler status PASS
.
Compiler status PASS
.
Compiler status PASS
output_nki=[[0.992188 1.27344 1.65625 ... 0.90625 1.34375 1.77344]
 [0 0.90625 1.34375 ... 0.390625 0.703125 0.914062]
 [0.5 0.390625 0.703125 ... 1.22656 1.15625 1.01562]
 ...
 [1.98438 1.98438 1.98438 ... 1.33594 1.64062 1.35938]
 [0.992188 1.33594 1.64062 ... 1.16406 1.67188 1.20312]
 [1.49219 1.16406 1.67188 ... 1.375 1 1.6875]]
.
Compiler status PASS
output_jax=[[0.992188 1.27344 1.65625 ... 0.90625 1.34375 1.77344]
 [0 0.90625 1.34375 ... 0.390625 0.703125 0.914062]
 [0.5 0.390625 0.703125 ... 1.22656 1.15625 1.01562]
 ...
 [1.98438 1.98438 1.98438 ... 1.33594 1.64062 1.35938]
 [0.992188 1.33594 1.64062 ... 1.16406 1.67188 1.20312]
 [1.49219 1.16406 1.67188 ... 1.375 1 1.6875]]
.
Compiler status PASS
NKI and JAX match
```


Note that the array values you see will differ from what’s printed
above, since this example uses jax.random.uniform to initialize the inputs.

## Download All Source Code

Click the links to download source code of the kernels and the testing code
discussed in this tutorial.

* NKI baremetal implementation: [`spmd_tensor_addition_nki_kernels.py`](../../downloads/spmd_tensor_addition_nki_kernels.py)

* 
PyTorch implementation: [`spmd_tensor_addition_torch.py`](../../downloads/spmd_tensor_addition_torch.py)

You must also download [`spmd_tensor_addition_nki_kernels.py`](../../downloads/spmd_tensor_addition_nki_kernels.py)
into the same folder to run this PyTorch script.

* 
JAX implementation: [`spmd_tensor_addition_jax.py`](../../downloads/spmd_tensor_addition_jax.py)

You must also download [`spmd_tensor_addition_nki_kernels.py`](../../downloads/spmd_tensor_addition_nki_kernels.py)
into the same folder to run this PyTorch script.

You can also view the source code in the GitHub repository [nki_samples](https://github.com/aws-neuron/nki-samples/tree/main/src/nki_samples/tutorials/tensor_addition/)

### Example usage of the scripts:

Run NKI baremetal implementation:


```python
python3 spmd_tensor_addition_nki_kernels.py
```


Run PyTorch implementation:


```python
python3 spmd_tensor_addition_torch.py
```


Run JAX implementation:


```python
python3 spmd_tensor_addition_jax.py
```