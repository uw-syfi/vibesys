# SPMD Tensor Addition using Multiple Neuron Cores

SPMD Tensor Addition using Multiple Neuron Cores
In this tutorial we reuse the [simple tensor addition kernel](spmd_tensor_addition.md#nki-tutorial-spmd-tensor-addition),
but directly control how our kernels and tensors are distributed across multiple neuron cores.

Doing so, we expand our knowledge about:

* The NKI syntax and the [Logical Neuron Cores (LNC)](../lnc.md#nki-about-lnc).

* nki.language.spmd_dim() and nki.language.nc()

## PyTorch

### Reusing existing compute kernel in helper function

We start by reusing the `nki_tensor_add_kernel_` compute kernel that has large tensor inputs,
but operates on a subset of the tensor at a tile size of `[128, 512]`.
The partition dimension tile size is chosen according to the tile size
restrictions (nki.language.tile_size.pmax),
while the free dimension tile size is chosen arbitrarily (`512`).


```python
def nki_tensor_add_nc2(a_input, b_input):
  """NKI kernel caller to compute element-wise addition of two input tensors using multiple Neuron cores.

  This kernel caller lifts tile-size restriction, by applying the kernel on tiles of the inputs/outputs.
  a_input and b_input are sharded across Neuron cores, directly utilizing Trn2 architecture capabilities

  Args:
      a_input: a first input tensor, of shape [N*128, M*512]
      b_input: a second input tensor, of shape [N*128, M*512]

  Returns:
      a tensor of shape [N*128, M*512], the result of a_input + b_input
  """

  # The SPMD launch grid denotes the number of kernel instances.
  # In this case, we use a 2D grid where the size of each invocation is 128x512
  # Since we're sharding across neuron cores on the 1st dimension we want to do our slicing at 
  # 128 per core * 2 cores = 256
  grid_x = a_input.shape[0] // (128 * 2)
  grid_y = a_input.shape[1] // 512

  # In addition, we distribute the kernel to physical neuron cores around the first dimension
  # of the spmd grid.
  # This means:
  # Physical NC [0]: kernel[n, m] where n is even
  # Physical NC [1]: kernel[n, m] where n is odd
  # notice, by specifying this information in the SPMD grid, we can use multiple neuron cores
  # without updating the original `nki_tensor_add_kernel_` kernel.
  return nki_tensor_add_kernel_[nl.spmd_dim(grid_x, nl.nc(2)), grid_y](a_input, b_input)
```


In this example:

* We reuse the NKI kernel in `nki_tensor_add_kernel_` which is decorated with the
nki.jit decorator to call the nki compiler to compile the kernel.

* Recall this kernel defines offsets into the tensors based on the ID of
the worker executing the code (`nl.program_id`), and generates tile
indices using these offsets with `nl.arange`.

* Using SPMD execution as discussed in [Logical Neuron Cores (LNC)](../lnc.md#nki-about-lnc),
note that each worker only operates on a (sub-tensor) tile of the
input/output tensors. By accessing its own `program_id`, each
worker can calculate the offsets it needs to access the correct
tiles.

* When multiple Neuron Cores are specified in the SPMD launch grid, these tensors are further
sharded across available cores. On Trainium 2, we have 2 local cores that have shared HBM.

* As before, the first axis of the tensor (mapped to the partition-dimension) is
tiled into blocks of 128, based on hardware restrictions (see [Tile
Size Considerations](../tiling-overview.md#nki-about-tiling)).
The second axis (mapped to the free-dimension) is tiled into blocks of 512 (no tile-size constraint,
since the addition operation is performed on the Vector engine, the only restriction is on-chip memory capacity).

* `nl.store` for kernels running on both cores will write to an `c_output` in
shared HBM, dramatically increasing the throughput of the computation.

### SPMD execution

* We want to shard the workload across 2 cores, so for every `nl.nc(2)` we determine our initial `axis=0` to be
`128` from the expected slice size in the kernel `*` the number of cores `= 256`.

* Thus we alter our previous sample and change `grid_x` to `a_input.shape[0] // (128 * 2)` to account for this.

* Launch the kernel with launch grid `[nl.spmd_dim(grid_x, nl.nc(2)), grid_y]`

As before, we are using a two-dimensional grid where the first dimension of the
tensor is tiled in the X dimension of the grid while the second
dimension is tiled in the Y dimension of the grid. We similarly
assume that tensor sizes are a multiple of maximum tile sizes allowed,
so we do not need to handle partial tiles.

However, this time we also directly specify how each instance of our kernel will be distributed
across multiple local Neuron Cores such that:


```python
# Physical NC [0]: kernel[n, m] where n is 0 or even
# Physical NC [1]: kernel[n, m] where n is odd
```


### Launching kernel and testing correctness

To execute the kernel, we prepare tensors `a` and `b`, and call the
`nki_tensor_add_nc2` helper function. We also verify the correctness of the NKI kernel against, torch by
comparing the outputs of both, using `torch.allclose`:


```python
import torch
from torch_xla.core import xla_model as xm

if __name__ == "__main__":
  device = xm.xla_device()

  a = torch.rand((512, 2048), dtype=torch.bfloat16).to(device=device)
  b = torch.rand((512, 2048), dtype=torch.bfloat16).to(device=device)

  output_nki = nki_tensor_add_nc2(a, b)
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
output_nki=tensor([[1.459  1.488  1.607  ... 1.217  0.7354 1.457 ]
      [1.793  0.7373 0.8877 ... 1.813  0.8936 1.39  ]
      [0.7285 0.9473 1.531  ... 1.04   1.302  0.8413]
      ...
      [0.7705 1.195  1.047  ... 1.307  0.588  0.7725]
      [1.21   1.719  1.209  ... 1.171  0.583  0.5034]
      [1.307  1.521  0.9526 ... 0.5825 1.518  0.673 ]],
       device='xla:1', dtype=torch.bfloat16)
2023-12-29 15:18:02.000219:  14463  INFO ||NEURON_CACHE||: Compile cache path: /var/tmp/neuron-compile-cache
2023-12-29 15:18:02.000220:  14463  INFO ||NEURON_CC_WRAPPER||: Call compiler with cmd: ['neuronx-cc', '--target=trn1', 'compile', '--framework', 'XLA', '/tmp/neuroncc_compile_workdir/2e135b73-1c3b-45e4-a6f0-2c4b105c20e5/model.MODULE_10032327759287407517+d41d8cd9.hlo.pb', '--output', '/tmp/neuroncc_compile_workdir/2e135b73-1c3b-45e4-a6f0-2c4b105c20e5/model.MODULE_10032327759287407517+d41d8cd9.neff', '--verbose=35']
.
Compiler status PASS
output_torch=tensor([[1.459  1.488  1.607  ... 1.217  0.7354 1.457 ]
      [1.793  0.7373 0.8877 ... 1.813  0.8936 1.39  ]
      [0.7285 0.9473 1.531  ... 1.04   1.302  0.8413]
      ...
      [0.7705 1.195  1.047  ... 1.307  0.588  0.7725]
      [1.21   1.719  1.209  ... 1.171  0.583  0.5034]
      [1.307  1.521  0.9526 ... 0.5825 1.518  0.673 ]],
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

### Helper function and SPMD execution

We can reuse the same NKI compute kernel defined for PyTorch above and declare a helper function
to launch the compute-kernel with appropriate grid/block sizes, to perform the computation:


```python
def nki_tensor_add_nc2(a_input, b_input):
  """NKI kernel caller to compute element-wise addition of two input tensors using multiple Neuron cores.

  This kernel caller lifts tile-size restriction, by applying the kernel on tiles of the inputs/outputs.
  a_input and b_input are sharded across Neuron cores, directly utilizing Trn2 architecture capabilities

  Args:
      a_input: a first input tensor, of shape [N*128, M*512]
      b_input: a second input tensor, of shape [N*128, M*512]

  Returns:
      a tensor of shape [N*128, M*512], the result of a_input + b_input
  """

  # The SPMD launch grid denotes the number of kernel instances.
  # In this case, we use a 2D grid where the size of each invocation is 128x512
  # Since we're sharding across neuron cores on the 1st dimension we want to do our slicing at 
  # 128 per core * 2 cores = 256
  grid_x = a_input.shape[0] // (128 * 2)
  grid_y = a_input.shape[1] // 512

  # In addition, we distribute the kernel to physical neuron cores around the first dimension
  # of the spmd grid.
  # This means:
  # Physical NC [0]: kernel[n, m] where n is even
  # Physical NC [1]: kernel[n, m] where n is odd
  # notice, by specifying this information in the SPMD grid, we can use multiple neuron cores
  # without updating the original `nki_tensor_add_kernel_` kernel.
  return nki_tensor_add_kernel_[nl.spmd_dim(grid_x, nl.nc(2)), grid_y](a_input, b_input)
```


As before, we are using a two-dimensional grid where the first dimension of the
tensor is tiled in the X dimension of the grid, while the second
dimension is tiled in the Y dimension of the grid. We similarly
assume that tensor sizes are a multiple of maximum tile sizes allowed,
so we do not need to handle partial tiles.

However, this time we also directly specify how each instance of our kernel will be distributed
across multiple local Neuron Cores such that:


```python
# Physical NC [0]: kernel[n, m] where n is 0 or even
# Physical NC [1]: kernel[n, m] where n is odd
```


### Launching kernel and testing correctness

To execute the kernel, we prepare arrays `a` and `b`, and call the
`nki_tensor_add_nc2` helper function. We also verify the correctness of the NKI kernel against, JAX by
comparing the outputs of both, using `jax.numpy.allclose`:


```python
import jax
import jax.numpy as jnp

if __name__ == "__main__":

  seed_a, seed_b = jax.random.split(jax.random.PRNGKey(42))
  a = jax.random.uniform(seed_a, (512, 2048), dtype=jnp.bfloat16)
  b = jax.random.uniform(seed_b, (512, 2048), dtype=jnp.bfloat16)

  output_nki = nki_tensor_add_nc2(a, b)
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

## Download all source code

Click the links to download source code of the kernels and the testing code
discussed in this tutorial.

* 
NKI baremetal implementation: [`spmd_multiple_nc_tensor_addition_nki_kernels.py`](../../downloads/spmd_multiple_nc_tensor_addition_nki_kernels.py)

You must also download [`spmd_tensor_addition_nki_kernels.py`](../../downloads/spmd_tensor_addition_nki_kernels.py)
into the same folder to run this script.

* 
PyTorch implementation: [`spmd_multiple_nc_tensor_addition_torch.py`](../../downloads/spmd_multiple_nc_tensor_addition_torch.py)

You must also download [`spmd_multiple_nc_tensor_addition_nki_kernels.py`](../../downloads/spmd_multiple_nc_tensor_addition_nki_kernels.py) and
[`spmd_tensor_addition_nki_kernels.py`](../../downloads/spmd_tensor_addition_nki_kernels.py)
into the same folder to run this PyTorch script.

* 
JAX implementation: [`spmd_multiple_nc_tensor_addition_jax.py`](../../downloads/spmd_multiple_nc_tensor_addition_jax.py)

You must also download [`spmd_multiple_nc_tensor_addition_nki_kernels.py`](../../downloads/spmd_multiple_nc_tensor_addition_nki_kernels.py) and
[`spmd_tensor_addition_nki_kernels.py`](../../downloads/spmd_tensor_addition_nki_kernels.py)
into the same folder to run this PyTorch script.

You can also view the source code in the GitHub repository [nki_samples](https://github.com/aws-neuron/nki-samples/tree/main/src/nki_samples/tutorials/tensor_addition/)

### Example usage of the scripts:

Run NKI baremetal implementation:


```python
python3 spmd_multiple_nc_tensor_addition_nki_kernels.py
```


Run PyTorch implementation:


```python
python3 spmd_multiple_nc_tensor_addition_torch.py
```


Run JAX implementation:


```python
python3 spmd_multiple_nc_tensor_addition_jax.py
```