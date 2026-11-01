# Matrix multiplication

Matrix multiplication
In this tutorial, we will start with a simple NKI matrix multiplication kernel
and optimize it step by step. In doing so, we learn about:

* The NKI syntax and programming model.

* Layout, tiling, and memory management considerations when performing
matrix multiplication in NKI.

## Basic compute kernel

!
> **Figure: matrix multiplication views**
>
> A diagram comparing mathematical matrix multiplication view with Tensor Engine view, showing how lhs and rhs matrices map to lhs_T (stationary in Tensor Engine), rhs (moving from SBUF), and output locations.
>
> This diagram illustrates the mapping between standard matrix multiplication notation and NeuronCore Tensor Engine execution, divided into two parts by a dashed line.
>
> Part (a) "Mathematical View" (left side) shows:
> - A blue matrix labeled "rhs" at the top with dimensions N (width) by K (height)
> - A green matrix labeled "lhs" at the bottom left with dimensions K (width) by M (height)
> - A purple matrix labeled "output" at the bottom right with dimensions N (width) by M (height)
> - This represents the standard lhs * rhs = output matrix multiplication
>
> Part (b) "Tensor Engine View" (right side) shows the hardware mapping:
> - A green matrix labeled "lhs_T (Tensor Engine)" with dimensions M (lhs_fsize) width by K (lhs_psize) height - the left-hand side transposed and loaded into Tensor Engine as the stationary matrix
> - A blue matrix labeled "rhs (SBUF)" with dimensions N (rhs_fsize) width by K (rhs_psize) height - the right-hand side stored in State Buffer as the moving matrix
> - A purple matrix labeled "output (PSUM)" with dimensions N (rhs_fsize) width by M (lhs_fsize) height - the output accumulates in Partial Sum buffer
> - Arrows show data flow from lhs_T and rhs into the output
> - A "Copy" arrow shows the PSUM output being copied to "output (SBUF)" with dimensions N width by M height
>
> Dimension annotations include:
> - M (lhs_fsize): Free dimension of left-hand side
> - N (rhs_fsize): Free dimension of right-hand side
> - K (lhs_psize, rhs_psize): Contraction/partition dimension
> - PSUM P-dim and SBUF P-dim labels
>
> **Key Elements:**
> - **Mathematical View (a)**: lhs * rhs = output multiplication
> - **Tensor Engine View (b)**: Hardware implementation view
> - **lhs_T (Tensor Engine)**: Transposed left matrix held stationary
> - **rhs (SBUF)**: Right matrix streaming from State Buffer
> - **output (PSUM)**: Partial sum accumulator
> - **output (SBUF)**: Final output in State Buffer
> - **Copy**: Data transfer from PSUM to SBUF
> - **Dimension labels**: M, N, K with fsize and psize annotations


Fig. 21 MxKxN Matrix Multiplication Visualization

[Fig. 21](#nki-fig-mm-view) illustrates how a simple matrix
multiplication: `lhs [M, K] * rhs [K, N] = output [M, N]` would be mapped to the
Tensor Engine (TensorE) and SRAMs from its original mathematical view. Note, the PSUM
partition dimension is rotated 90 degrees from SBUF partition dimension solely for layout visualization.
The copy preserves the `output` tile layout from PSUM to SBUF, by copying data from each PSUM partition
to the corresponding SBUF partition.

The NKI example below implements a compute kernel for a single-tile matrix
multiplication. It computes a `64(M) x 128(K) x 512 (N)` matrix
multiplication operation.


```python
@nki.jit
def nki_matmul_basic_(lhsT, rhs):
  """NKI kernel to compute a 64x128x512 matrix multiplication operation

  Args:
      lhsT: an input tensor of shape [128,64], a left hand side argument of the
        matrix multiplication, delivered transposed for optimal performance
      rhs: an input tensor of shape [128,512], a right hand side argument of the
        matrix multiplication
  Returns:
      result: the resulting output tensor of shape [64,512]
  """
  # Verify that the lhsT and rhs are the expected sizes.
  K, M = lhsT.shape
  K_, N = rhs.shape

  # Check that the contraction dimension matches and all dimensions
  #are what were expected.
  assert K == K_, \
    f"Expected contraction dimension to match on both lhsT ({K}) and rhs ({K})"
  assert K == 128, f"Expected contraction dimension to be 128, but got {K}"
  assert M == 64, f"Expected lhsT matrix to have dimension M of 64, but got {M}"
  assert N == 512, f"Expected rhs matrix to have dimension N of 512, but got {N}"

  # Create a tensor to write the result into (not initialized)
  result = nl.ndarray((M, N), dtype=lhsT.dtype, buffer=nl.shared_hbm)

  # Creating a tensor in SBUF to load the inputs into (not initialized)
  lhs_tile = nl.ndarray(lhsT.shape, dtype=lhsT.dtype, buffer=nl.sbuf)
  rhs_tile = nl.ndarray(rhs.shape, dtype=rhs.dtype, buffer=nl.sbuf)

  # Loading the inputs (HBM->SBUF)
  # Note: here we take Tile dtype definition into account,
  # which forces P-dim as the left most index
  nisa.dma_copy(dst=lhs_tile, src=lhsT)
  nisa.dma_copy(dst=rhs_tile, src=rhs)

  # Create a tensor in PSUM to accumulate the result in (uninitialized)
  result_psum = nl.ndarray(result.shape, dtype=nl.float32, buffer=nl.psum)

  # Perform the matrix-multiplication
  # Note: A NKI matmul instruction always writes to PSUM in float32 data-type
  nisa.nc_matmul(result_psum, lhs_tile, rhs_tile)

  # Create a tensor in SBUF and copy the result from PSUM back to SBUF, 
  # and cast to expected output data-type
  result_sbuf = nl.ndarray(result_psum.shape, dtype=result.dtype, buffer=nl.sbuf)
  nisa.tensor_copy(dst=result_sbuf, src=result_psum, dtype=result.dtype)

  # The result of [64,128] x [128,512] matrix multiplication has a shape of [64, 512].
  # This dictates which indices to use to address the result tile.
  nisa.dma_copy(dst=result, src=result_sbuf)

  return result
```


In this example, we define the NKI kernel as `nki_matmul_basic_:`

* We define indices to access the LHS and RHS input tensors.

* To adhere to NKI’s layout considerations,
we map the contraction axis of both LHS and RHS to the P-dimension,
which means we load LHS in transposed form.

* To adhere to NKI’s tile size considerations,
we limit the matmul instruction arguments to tiles of up to
`[128,128]` for LHS, and `[128,512]` for RHS.

* Using the `nisa.dma_copy` operation, we load the inputs from HBM tensors
to SBUF tiles.

* We then use the `nisa.nc_matmul` operation to perform the matrix
multiplication. Note that we set the LHS argument is transposed. Also note that the *64x128*
dimension here actually under-utilizes the TensorE, but it helps to
distinguish the M, K and N dimensions for education purposes in this first
code example.

* `nisa.nc_matmul` always writes its result to PSUM, and since
`nisa.dma_copy` only moves data from SBUF to HBM, we copy the
multiplication result from PSUM back to SBUF using `nisa.tensor_copy`.

We can then execute the kernel and verify correctness against the torch
implementation as follows. Note that we use torch.allclose to tolerate
numerical error inherent to floating-point arithmetic.


```python
device = xm.xla_device()
cpu = torch.device('cpu')

# Test the small workload with basic kernel
lhs_small = torch.rand((64, 128), dtype=torch.bfloat16, device=device)
rhs_small = torch.rand((128, 512), dtype=torch.bfloat16, device=device)

# Run NKI kernel
output_small = nki_matmul_basic_(lhs_small.T, rhs_small)

# Run torch reference
output_small_torch = torch.matmul(lhs_small, rhs_small)

# Compare results
print("Checking correctness of nki_matmul_basic")
if torch.allclose(output_small_torch, output_small, atol=1e-4, rtol=1e-2):
  print("NKI and Torch match")
else:
  print("NKI and Torch differ")
```


## Tiling matrix multiplications

So far, we’ve limited our matrix multiplication to the tile sizes
allowed by NKI’s tile size and layout constraints. Next, we’ll see how
to handle larger matrix multiplications. Let’s start with a pseudo-code
for tiling an `[M,K] &#64; [K,N]` matrix-multiplication.
Note that we assume the left-hand-side matrix (`[M,K]`) is already transposed
to LHS_T (`[K,M]`) for optimal performance of the underlying TensorE.


```python
# LHS_T: left-hand-side matmul argument (shape [K,M])
# RHS: right-hand-side matmul argument (shape [K,N])
# RES: matmul result (shape [M,N])

# Tile LHS_T free dimension
for m in range(0, M, 128):
  # Tile RHS free dimension
  for n in range(0, N, 512):
    # Zero-out the accumulator buffer
    accum = zeros((128, 512))
    # Tile contraction dimension
    for k in range(0, K, 128):
      lhsT_tile = LHS_T[m : m+128, k : k+128]
      rhs_tile = RHS[k : k+128, n : n+512]
      accum += dot(lhsT_tile, rhs_tile)
    RES[m : m+128, n : n+512] = accum
```


This form of tiling can be achieved in NKI as follows:


```python
@nki.jit
def nki_matmul_tiled_(lhsT, rhs):
  """NKI kernel to compute a matrix multiplication operation in a tiled manner

  Args:
      lhsT: an input tensor of shape [K,M], where both K and M are multiples for
        128.  It is the left-hand-side argument of the matrix multiplication,
        delivered transposed for optimal performance.
      rhs: an input tensor of shape [K,N], where K is a multiple of 128, and N
        is a multiple of 512.  It is the right-hand-side argument of the matrix
        multiplication.
  Returns:
      result: the resulting output tensor of shape [M,N]
  """

  # Verify that the lhsT and rhs have the same contraction dimension.
  K, M = lhsT.shape
  K_, N = rhs.shape
  assert K == K_, "lhsT and rhs must have the same contraction dimension"

  # Lookup the device matrix multiply dimensions.
  TILE_M = nl.tile_size.gemm_stationary_fmax  # 128
  TILE_K = nl.tile_size.pmax  # 128
  TILE_N = nl.tile_size.gemm_moving_fmax  # 512

  # Verify that the input matrices are a multiple of the tile dimensions.
  assert M % TILE_M == 0, \
    f"Expected M, {M}, to be a multiple of stationary free-dimension max, {TILE_M}"
  assert N % TILE_N == 0, \
    f"Expected N, {N}, to be a multiple of moving free-dimension max, {TILE_N}"
  assert K % TILE_K == 0, \
    f"Expected K, {K}, to be a multiple of the partition dimension max, {TILE_K}"

  # Create a space for the result in HBM (not initialized)
  result = nl.ndarray((M, N), dtype=lhsT.dtype, buffer=nl.shared_hbm)

  # Loop over tiles
  for m in range(M // TILE_M):
    for n in range(N // TILE_N):
      # Allocate a tensor in PSUM
      res_psum = nl.ndarray((TILE_M, TILE_N), nl.float32, buffer=nl.psum)

      for k in range(K // TILE_K):
        # Declare the tiles on SBUF
        lhsT_tile = nl.ndarray((TILE_K, TILE_M), dtype=lhsT.dtype, buffer=nl.sbuf)
        rhs_tile = nl.ndarray((TILE_K, TILE_N), dtype=rhs.dtype, buffer=nl.sbuf)

        # Load tiles from lhsT and rhs
        nisa.dma_copy(dst=lhsT_tile,
                      src=lhsT[k * TILE_K:(k + 1) * TILE_K,
                               m * TILE_M:(m + 1) * TILE_M])
        nisa.dma_copy(dst=rhs_tile, 
                      src=rhs[k * TILE_K:(k + 1) * TILE_K,
                              n * TILE_N:(n + 1) * TILE_N])

        # Accumulate partial-sums into PSUM
        nisa.nc_matmul(dst=res_psum, stationary=lhsT_tile, moving=rhs_tile)

      # Copy the result from PSUM back to SBUF, and cast to expected output data-type
      res_sb = nl.ndarray(res_psum.shape, dtype=result.dtype, buffer=nl.sbuf)
      nisa.tensor_copy(dst=res_sb, src=res_psum, dtype=result.dtype)

      # Copy the result from SBUF to HBM.
      nisa.dma_copy(dst=result[m * TILE_M:(m + 1) * TILE_M,
                               n * TILE_N:(n + 1) * TILE_N],
                    src=res_sb)

  return result
```


A few notes about the above code example:


```python
psum_buf = nl.ndarray(..., buffer=nl.psum)

# loop over tiles of the contraction dimension
for i in range(N):
   # add matmul results from TensorEngine
   nisa.nc_matmul(psum_buf, stationary_tile, moving_tile) # or nl.matmul
```


The use of [PSUM accumulation architecture feature](../../architecture/trainium_inferentia2_arch.md#arch-sec-accumulation-psum) is critical to
achieve good performance out of TensorEngine when
the contraction dimension of the matmul is greater than 128.

The first `nisa.nc_matmul` call overwrites the contents of the `psum_buf`, with
subsequent calls to the `nisa.nc_matmul` instruction accumulating results
into the `psum_buf`.

There is an alternative way to implement this tiled matrix multiplication kernel
using the SPMD programming model. We can use the SPMD model to launch `(M/128)
x (N/512)` instances of the kernel to complete the innermost loop.

## Optimization 1: Removing Redundant Loads

Currently, every `nisa.nc_matmul` is accompanied with two `nisa.dma_copy` calls in the
inner loop, both of which move data from HBM to SBUF. Let’s introduce a metric,
arithmetic intensity, to help understand why this is problematic. The arithmetic
intensity of a workload is defined as the number of computation operations
performed per byte of data accessed from HBM on average. The reason why we do
not consider data accessed from SBUF in this metric is because the SBUF
bandwidth (~20x higher than HBM) is high enough to sustain the peak computation
throughput in TensorE.

![../../../_images/roofline.png](../../../_images/roofline.png)

Fig. 22 Roofline Model: The Relationship Between Arithmetic Intensity and Performance

[Fig. 22](#nki-fig-roofline) shows the roofline model, which models the
relationship between arithmetic intensity of a workload and its achievable
performance on a given computing platform. To saturate TensorE in a
NeuronCore-v2, the arithmetic intensity threshold of a workload is 222
Flops/Byte for `bfloat16` data type. Inside the inner loop of
`nki_matmul_tiled_`, accessing `lhsT_tile` and `rhs_tile` requires
160 KB of data read from HBM, while the `nisa.nc_matmul` call involves 16 MFlops.
This leads to an arithmetic intensity of 102, which is significantly lower than
the saturation threshold of 222. Therefore, `nki_matmul_tiled_`
operates in the memory bound region of the roofline model and under-utilizes
TensorE. To make the best out of TensorE, we need to improve the arithmetic
intensity of the matmul kernel.

With NKI, programmers can control when and how to load data from HBM into SBUF
and also perform computation. We will demonstrate in the upcoming steps how to
increase the arithmetic intensity of the matmul kernel using NKI, thereby
maximizing the utilization of TensorE.

First, we notice that in `nki_matmul_tiled_`, the same tiles from
`lhsT` and `rhs` matrices are loaded more than once across different
iterations of the inner loop. The following example reduces these redundant
loads through hoisting them out of the innermost loop.

!
> **Figure: mm memory pattern after load hoisting**
>
> A diagram showing the memory access pattern after load hoisting optimization for matrix multiplication, with labeled tiles (LHS tile_00, RHS tile_00, Result tile_00) and specific dimension annotations (128, 512).
>
> This diagram illustrates the memory access pattern for matrix multiplication after applying the load hoisting optimization, with specific tile sizes annotated.
>
> The left matrix has dimensions M (height) by K (width), displayed as a 6x5 grid. The upper-left tile is highlighted in solid orange and labeled "LHS tile_00" with dimensions 128 (width) by 128 (height), representing a square tile of the left-hand side operand.
>
> The middle matrix has dimensions K (height) by N (width), displayed as a 5x6 grid. Two regions are highlighted:
> - A column labeled "RHS tile_00" in solid orange on the left side with dimensions 512 (width) by 128 (height)
> - Adjacent light blue columns showing additional tiles that will be reused
>
> The annotation "512" appears above for the N dimension, and "128" appears for the K dimension.
>
> The right matrix has dimensions M (height) by N (width), displayed as a 6x6 grid. The upper-left tile is highlighted in light blue and labeled "Result tile_00" with dimensions 512 (width) by 128 (height).
>
> This pattern shows the load hoisting optimization where:
> - LHS tiles are loaded and reused across multiple RHS tiles
> - RHS tiles share the K dimension with LHS
> - Result tiles are larger in the N dimension due to accumulating multiple partial products
>
> The specific dimensions (128, 512) suggest typical tile sizes for NeuronCore Tensor Engine operations.
>
> **Key Elements:**
> - **LHS tile_00**: Left operand tile (128 x 128) in orange
> - **RHS tile_00**: Right operand tile (128 x 512) in orange
> - **Result tile_00**: Output tile (128 x 512) in light blue
> - **M, K, N labels**: Matrix dimension annotations
> - **128, 512 dimensions**: Specific tile sizes in elements
> - **Light blue columns**: Additional RHS tiles showing reuse pattern


Fig. 23 Memory Pattern After Hoisting Loads Out of the Innermost Loop


```python
@nki.jit
def nki_matmul_hoist_load_(lhsT, rhs):
  """NKI kernel to compute a matrix multiplication operation in a tiled manner
     while hoisting the load of the lhsT and rhs to outer loops.

  Args:
      lhsT: an input tensor of shape [K,M], where both K and M are multiples for
        128.  It is the left-hand-side argument of the matrix multiplication,
        delivered transposed for optimal performance.
      rhs: an input tensor of shape [K,N], where K is a multiple of 128, and N
        is a multiple of 512.  It is the right-hand-side argument of the matrix
        multiplication.
  Returns:
      result: the resulting output tensor of shape [M,N]
  """

  # Verify that the lhsT and rhs are the expected sizes.
  K, M = lhsT.shape
  K_, N = rhs.shape
  assert K == K_, "lhsT and rhs must have the same contraction dimension"

  # Lookup the device matrix multiply dimensions.
  TILE_M = nl.tile_size.gemm_stationary_fmax  # 128
  TILE_K = nl.tile_size.pmax  # 128
  TILE_N = nl.tile_size.gemm_moving_fmax  # 512

  # Verify that the input matrices are a multiple of the tile dimensions.
  assert M % TILE_M == 0, \
    f"Expected M, {M}, to be a multiple of stationary free-dimension max, {TILE_M}"
  assert N % TILE_N == 0, \
    f"Expected N, {N}, to be a multiple of moving free-dimension max, {TILE_N}"
  assert K % TILE_K == 0, \
    f"Expected K, {K}, to be a multiple of the partition dimension max, {TILE_K}"

  # Create a space for the result in HBM (not initialized)
  result = nl.ndarray((M, N), dtype=lhsT.dtype, buffer=nl.shared_hbm)

  # Loop over tiles
  for m in range(M // TILE_M):
    # Load a whole column tiles from lhsT (with K * TILE_M numbers)
    # This corresponds to the whole row in the original lhs
    lhsT_tiles = []
    for k in range(K // TILE_K):
      # Allocate space in SBUF for the tile (uninitialized)
      lhsT_tile = nl.ndarray(shape=(TILE_K, TILE_M), dtype=lhsT.dtype, buffer=nl.sbuf)
      # Copy the tile from HBM to SBUF
      nisa.dma_copy(dst=lhsT_tile, 
                    src=lhsT[k * TILE_K:(k + 1) * TILE_K,
                             m * TILE_M:(m + 1) * TILE_M])
      # Append the tile to the list of tiles.
      lhsT_tiles.append(lhsT_tile)

    for n in range(N // TILE_N):
      # Load a whole column tiles from rhs (with K * TILE_N numbers)
      rhs_tiles = []
      for k in range(K // TILE_K):
        # Allocate space in SBUF for the tile (uninitialized)
        rhs_tile = nl.ndarray(shape=(TILE_K, TILE_N), dtype=rhs.dtype, buffer=nl.sbuf)
        # Copy the tile from HBM to SBUF
        nisa.dma_copy(dst=rhs_tile,
                      src=rhs[k * TILE_K:(k + 1) * TILE_K,
                              n * TILE_N:(n + 1) * TILE_N])
        # Append the tile to the list of tiles.
        rhs_tiles.append(rhs_tile)

      # Allocate a tile in PSUM for the result (uninitialized)
      res_psum = nl.ndarray(shape=(TILE_M, TILE_N), dtype=nl.float32, buffer=nl.psum)
      for k in range(K // TILE_K):
        # Accumulate partial-sums into PSUM
        nisa.nc_matmul(dst=res_psum, stationary=lhsT_tiles[k], moving=rhs_tiles[k])

      # Copy the result from PSUM back to SBUF, and cast to expected output data-type
      res_sb = nl.ndarray(shape=(TILE_M, TILE_N), dtype=nl.float32, buffer=nl.sbuf)
      nisa.tensor_copy(dst=res_sb, src=res_psum, dtype=result.dtype)

      # Copy the result from SBUF to HBM.
      nisa.dma_copy(dst=result[m * TILE_M:(m + 1) * TILE_M,
                               n * TILE_N:(n + 1) * TILE_N],
                    src=res_sb)

  return result
```


## Optimization 2: Blocking M and N Dimension

While hoisting the load out of the innermost loop eliminates some redundant
loads, we can push this idea further to increase arithmetic intensity.

Each time we load K elements from the MxK matrix stored in HBM, Optimization 1 allows us
to utilize those same elements N different times.
However, SBUF capacity is much higher than K elements currently cached from optimization 1.
We can load multiple K elements from the MxK matrix at a time, result in higher data reuse.
This will increase arithmetic intensity.

Block size must balance two constraints: it should be large enough to saturate arithmetic intensity, yet
small enough for all live blocks remain within SBUF capacity to avoid spilling, causing performance regression.

[Fig. 24](#nki-fig-mm-after-blocking-free) below visualizes the memory pattern
after blocking both free dimensions.

!
> **Figure: mm memory pattern after blocking free**
>
> A diagram showing the memory access pattern after blocking only the free dimension for matrix multiplication, with highlighted rows/columns showing the tiles accessed for each matrix operand and output.
>
> This diagram illustrates the memory access pattern for matrix multiplication after applying blocking to the free dimension only. Three matrices are shown side by side.
>
> The left matrix has dimensions M (height) by K (width), displayed as a 6x5 grid. The top two rows are entirely highlighted in solid orange, representing accessing full rows of the left operand along the K dimension.
>
> The middle matrix has dimensions K (height) by N (width), displayed as a 5x6 grid. A vertical stripe (columns 2-3) spanning the full K height is highlighted in solid orange, representing accessing full columns of the right operand.
>
> The right matrix has dimensions M (height) by N (width), displayed as a 6x6 grid. A 2x2 block in the upper-middle area is highlighted in solid orange, representing the output tile being computed.
>
> This pattern shows blocking along the free dimensions (M for left matrix, N for right matrix) while iterating over the full contraction dimension K. The highlighted regions demonstrate:
> - Full rows of the left matrix are loaded (M blocked, K unblocked)
> - Full columns of the right matrix are loaded (K unblocked, N blocked)
> - A small tile of output is produced
>
> This approach reduces output memory traffic but requires loading more input data per output tile compared to blocking all dimensions.
>
> **Key Elements:**
> - **Left matrix (M x K)**: First operand with top 2 rows fully highlighted
> - **Middle matrix (K x N)**: Second operand with 2 middle columns fully highlighted
> - **Right matrix (M x N)**: Output matrix with 2x2 tile highlighted
> - **M, K, N labels**: Dimension annotations
> - **Orange highlighting**: Tiles accessed in this computation step
> - **Full row/column access**: Shows K dimension not blocked


Fig. 24 Memory Pattern After Blocking Free Dimensions


```python
@nki.jit
def nki_matmul_block_free_dimension_(lhsT, rhs):
  """NKI kernel to compute a matrix multiplication operation while blocking the
     free dimensions of the LHS and RHS to improve memory access pattern.

  Args:
      lhsT: an input tensor of shape [K,M], where both K and M are multiples for
        128.  It is the left-hand-side argument of the matrix multiplication,
        delivered transposed for optimal performance.
      rhs: an input tensor of shape [K,N], where K is a multiple of 128, and N
        is a multiple of 512.  It is the right-hand-side argument of the matrix
        multiplication.
  Returns:
      result: the resulting output tensor of shape [M,N]
  """

  # Verify that the lhsT and rhs have the same contraction dimension.
  K, M = lhsT.shape
  K_, N = rhs.shape
  assert K == K_, "lhsT and rhs must have the same contraction dimension"

  # Lookup the device matrix multiply dimensions.
  TILE_M = nl.tile_size.gemm_stationary_fmax  # 128
  TILE_K = nl.tile_size.pmax  # 128
  TILE_N = nl.tile_size.gemm_moving_fmax  # 512

  # Configuring the blocking size for the free dimensions
  TILES_IN_BLOCK_M = 2
  TILES_IN_BLOCK_N = 2

  BLOCK_M = TILE_M * TILES_IN_BLOCK_M  # 256
  BLOCK_N = TILE_N * TILES_IN_BLOCK_N  # 1024

  # the size has to be multiple of block size
  assert M % BLOCK_M == 0
  assert N % BLOCK_N == 0

  # Create a space for the result in HBM (not initialized)
  result = nl.ndarray((M, N), dtype=lhsT.dtype, buffer=nl.shared_hbm)

  # Loop over blocks over the M dimension
  for m in range(M // BLOCK_M):
    # Load TILES_IN_BLOCK_M columns tiles by TILES_K rows from lhsT
    lhsT_tiles = []
    for bm in range(TILES_IN_BLOCK_M):
      # Inner tile array.
      lhsT_tiles_internal = []
      for k in range(K // TILE_K):
        # Allocate space in SBUF for the tile (uninitialized)
        lhsT_tile = nl.ndarray(shape=(TILE_K, TILE_M),
                               dtype=lhsT.dtype,
                               buffer=nl.sbuf)
        # Copy the tile from HBM to SBUF
        nisa.dma_copy(dst=lhsT_tile,
                      src=lhsT[k * TILE_K:(k + 1) * TILE_K,
                               (m * TILES_IN_BLOCK_M + bm) *
                               TILE_M:((m * TILES_IN_BLOCK_M + bm) + 1) *
                               TILE_M])
        # Append the tile to the inner list of tiles.
        lhsT_tiles_internal.append(lhsT_tile)
      # Append the inner list of tiles into the outer list of tiles.
      lhsT_tiles.append(lhsT_tiles_internal)

    for n in range(N // BLOCK_N):
      # Load TILES_IN_BLOCK_N columns from rhs by TILES_K rows from rhs
      rhs_tiles = []
      for bn in range(TILES_IN_BLOCK_N):
        # Inner tile array.
        rhs_tiles_internal = []
        for k in range(K // TILE_K):
          # Allocate space in SBUF for the tile (uninitialized)
          rhs_tile = nl.ndarray(shape=(TILE_K, TILE_N),
                                dtype=rhs.dtype,
                                buffer=nl.sbuf)
          # Copy the tile from HBM to SBUF
          nisa.dma_copy(dst=rhs_tile,
                        src=rhs[k * TILE_K:(k + 1) * TILE_K,
                                (n * TILES_IN_BLOCK_N + bn) *
                                TILE_N:((n * TILES_IN_BLOCK_N + bn) + 1) *
                                TILE_N])
          # Append the tile to the inner list of tiles.
          rhs_tiles_internal.append(rhs_tile)
        # Append the inner list of tiles into the outer list of tiles.
        rhs_tiles.append(rhs_tiles_internal)

      for bm in range(TILES_IN_BLOCK_M):
        for bn in range(TILES_IN_BLOCK_N):
          # Allocate a tensor in PSUM
          result_tile = nl.ndarray(shape=(TILE_M, TILE_N),
                                   dtype=nl.float32,
                                   buffer=nl.psum)
          for k in range(K // TILE_K):
            # Accumulate partial-sums into PSUM
            nisa.nc_matmul(dst=result_tile,
                           stationary=lhsT_tiles[bm][k],
                           moving=rhs_tiles[bn][k])
  
          # Copy the result from PSUM back to SBUF, and cast to expected
          # output data-type
          result_tmp = nl.ndarray(shape=result_tile.shape,
                                  dtype=result.dtype,
                                  buffer=nl.sbuf)
          nisa.tensor_copy(dst=result_tmp, src=result_tile)

          # Copy the result from SBUF to HBM.
          nisa.dma_copy(dst=result[(m * TILES_IN_BLOCK_M + bm) *
                                   TILE_M:((m * TILES_IN_BLOCK_M + bm) + 1) *
                                   TILE_M,
                                   (n * TILES_IN_BLOCK_N + bn) *
                                   TILE_N:((n * TILES_IN_BLOCK_N + bn) + 1) *
                                   TILE_N],
                        src=result_tmp)

  return result
```


## Optimization 3: Blocking M, N and K Dimension

Blocking only free dimension and requiring to load the whole partition dimension (K) will set an upper
limit on block size (M and N) due to limited SBUF capacity.

Matrix multiply with shapes [M, K] &#64; [K, N] = [M, N] requires K multiplies and K additions
(or K-1 for accumulation) for each element in resulting [M, N] grid, totaling 2*K*M*N FLOPS.
It has to load M*K + K*N + M*N elements, resulting in arithemtic intensity 2*M*N*K/(2*(M*K + K*N + M*N))
for 2 byte data type like FP16 or BF16. Since the full K has to fit in memory for optimization 2,
it will limit M and N size for a block. Arithmetic intensity will be lower any of the M, N or K is
much smaller than the others.

Blocking partition dimension also results in calculating partial matrix multiplies in each block that have to
be accumulated, resulting in addintional HBM traffic if not handled carefully.

!
> **Figure: mm memory pattern after blocking all**
>
> A diagram showing the memory access pattern after blocking optimization for matrix multiplication, with three matrices (M x K, K x N, and M x N) where blocked tiles are highlighted in solid orange and dotted orange patterns.
>
> This diagram illustrates the memory access pattern for matrix multiplication after applying blocking optimizations to both dimensions. Three matrices are shown side by side.
>
> The left matrix has dimensions M (height) by K (width), displayed as a 6x5 grid. A 2x2 block of cells in the upper-left area is highlighted in solid orange, representing a tile of the left-hand operand being accessed.
>
> The middle matrix has dimensions K (height) by N (width), displayed as a 5x6 grid. A 2x2 block in the upper-middle area is highlighted in solid orange, representing the corresponding tile of the right-hand operand.
>
> The right matrix has dimensions M (height) by N (width), displayed as a 6x6 grid. A 3x3 block in the upper-left area is filled with a dotted orange pattern, representing the output tile being computed. The dotted pattern (rather than solid) may indicate this is the accumulation destination rather than a source being read.
>
> All three matrices show their dimension labels: M on the vertical axis of the left and right matrices, K on the horizontal axis of the left matrix and vertical axis of the middle matrix, and N on the horizontal axis of the middle and right matrices.
>
> The highlighted regions show how blocking divides the computation into smaller tiles that fit in on-chip memory, improving data locality and reducing HBM bandwidth requirements.
>
> **Key Elements:**
> - **Left matrix (M x K)**: First operand with 2x2 solid orange tile highlighted
> - **Middle matrix (K x N)**: Second operand with 2x2 solid orange tile highlighted
> - **Right matrix (M x N)**: Output matrix with 3x3 dotted orange tile
> - **M, K, N labels**: Dimension annotations on each matrix
> - **Solid orange tiles**: Input tiles being accessed
> - **Dotted orange tile**: Output tile being accumulated
> - **Grid structure**: Shows tiling/blocking boundaries


Fig. 25 Memory Pattern After Blocking All Dimensions

With the blocking configuration in the code (16 tiles or 2048 numbers in the
`M` dimension; 2 tiles or 1024 numbers in the `N` dimension; and 8 tiles or
1024 numbers in the `K` dimension), this computation has an arithmetic
intensity of 683 Flops/Byte (2048*1024*1024/(2048*1024 + 1024*1024)). This is
certainly above the threshold of 222.

At the same time, this blocking configuration keeps all the tensors within the
SBUF limit as much as possible. With all matrices in BF16 data type, the
`lhsT_tiles` requires 4MB and `rhs_tiles` requires 2MB SBUF memory. The
`result_tiles` requires `4 * NUM_BLOCK_M` MB SBUF memory, where
`NUM_BLOCK_M` is `M // 2048`. Thus, as long as `M <= 8192`, the required
SBUF memory is under the 24 MB budget (4 + 2 + 4 * (8192 // 2048) == 22 MB).
When the `M` dimension becomes bigger, spilling and reloading of the
`result_tiles` will happen, but because the frequency is relatively low, the
computation can still be sufficient.
Block size must balance two constraints: it should be large enough to saturate arithmetic intensity, yet
small enough for all live blocks remain within SBUF capacity to avoid spilling, causing performance regression.

The K blocking loop is hand optimized for our ideal data locality.


```python
@nki.jit
def nki_matmul_fully_optimized_(
    lhsT,
    rhs,
    # Meta-parameters
    TILES_IN_BLOCK_M=16,
    TILES_IN_BLOCK_N=2,
    TILES_IN_BLOCK_K=8,
):
  """NKI kernel to compute a large matrix multiplication efficiently by
     blocking all dimensions and doing layout optimization.

  Args:
      lhsT: an input tensor of shape [K,M], where K is a multiple of 128 *
        TILES_IN_BLOCK_K and M is a multiple of 128 * TILES_IN_BLOCK_M.  It is the
        left-hand-side argument of the matrix multiplication, delivered transposed
        for optimal performance.
      rhs: an input tensor of shape [K,N],  where K is a multiple of 128 *
        TILES_IN_BLOCK_K and N is a multiple of 512 * TILES_IN_BLOCK_N.  It is
        the right-hand-side argument of the matrix multiplication.
      TILES_IN_BLOCK_*: meta parameters to control blocking dimensions
  Returns:
      result: the resulting output tensor of shape [M,N]
  """

  # Verify that the lhsT and rhs have the same contraction dimension.
  K, M = lhsT.shape
  K_, N = rhs.shape
  assert K == K_, "lhsT and rhs must have the same contraction dimension"

  # Lookup the device matrix multiply dimensions.
  TILE_M = nl.tile_size.gemm_stationary_fmax  # 128
  TILE_K = nl.tile_size.pmax  # 128
  TILE_N = nl.tile_size.gemm_moving_fmax  # 512

  # Compute the block dimensions.
  BLOCK_M = TILE_M * TILES_IN_BLOCK_M
  BLOCK_N = TILE_N * TILES_IN_BLOCK_N
  BLOCK_K = TILE_K * TILES_IN_BLOCK_K

  # Verify the size is a multiple of block size
  assert M % BLOCK_M == 0, \
    f"Expected M {M} to be divisble by {BLOCK_M} when there are {TILES_IN_BLOCK_M}"
  assert N % BLOCK_N == 0, \
    f"Expected N {N} to be divisble by {BLOCK_N} when there are {TILES_IN_BLOCK_N}"
  assert K % BLOCK_K == 0, \
    f"Expected K {K} to be divisble by {BLOCK_K} when there are {TILES_IN_BLOCK_K}"

  # Create a space for the result in HBM (not initialized)
  result = nl.ndarray((M, N), dtype=lhsT.dtype, buffer=nl.shared_hbm)

  # Compute the number of blocks in each dimension
  NUM_BLOCK_M = M // BLOCK_M
  NUM_BLOCK_N = N // BLOCK_N
  NUM_BLOCK_K = K // BLOCK_K

  # Blocking N dimension (the RHS free dimension)
  for n in range(NUM_BLOCK_N):
    # Create the initial result tiles in SBUF and initialize each tile to
    # 0.0, since the final results will be accumulated here. Results in 3-d array.
    result_tmps = []
    for m_idx in range(NUM_BLOCK_M):
      block_m = []
      for bm_idx in range(TILES_IN_BLOCK_M):
        block_n = []
        for bn_idx in range(TILES_IN_BLOCK_N):
          # Create the result tile (uninitialized)
          tile = nl.ndarray(shape=(TILE_M, TILE_N), dtype=lhsT.dtype, buffer=nl.sbuf)
          # Initialize the tile 0.0
          nisa.memset(dst=tile, value=0.0)
          # Append the tile to block_n array.
          block_n.append(tile)
        # Append block_n array to block_m array.
        block_m.append(block_n)
      # Append block_m array into result_tmps.
      result_tmps.append(block_m)

    # Blocking K dimension (the contraction dimension)
    for k in range(NUM_BLOCK_K):
      # Loading tiles from rhs
      # setting the load tile to `TILE_K x BLOCK_SIZE_N` to optimize DMA performance
      rhs_tiles = []
      for bk_r in range(TILES_IN_BLOCK_K):
        # Allocate rhs_tile tensor, TILE_K x BLOCK_N
        rhs_tile = nl.ndarray(shape=(TILE_K, BLOCK_N),
                              dtype=rhs.dtype,
                              buffer=nl.sbuf)
        # Copy block tile from rhs, to rhs_tile.
        nisa.dma_copy(dst=rhs_tile[0:TILE_K, 0:BLOCK_N],
                      src=rhs[(TILES_IN_BLOCK_K * k + bk_r) *
                              TILE_K:(TILES_IN_BLOCK_K * k + bk_r + 1) * TILE_K,
                              BLOCK_N * n:BLOCK_N * (n + 1)])
        # Append rhs_tile to rhs_tiles.
        rhs_tiles.append(rhs_tile)


      # Blocking M dimension (the LHS free dimension)
      for m in range(NUM_BLOCK_M):
        # Loading tiles from lhsT
        lhsT_tiles = []
        for bk_l in range(TILES_IN_BLOCK_K):
          # Allocate lhsT_tile in SBUF (uninitialized)
          lhsT_tile = nl.ndarray(shape=(TILE_K, BLOCK_M),
                                 dtype=lhsT.dtype,
                                 buffer=nl.sbuf)
          # Copy block tile from lhsT to lhsT_tile
          nisa.dma_copy(dst=lhsT_tile[0:TILE_K, 0:BLOCK_M],
                        src=lhsT[(TILES_IN_BLOCK_K * k + bk_l) *
                                 TILE_K:(TILES_IN_BLOCK_K * k + bk_l + 1) * TILE_K,
                                 BLOCK_M * m:BLOCK_M * (m + 1)])
          # Append to list of lhsT tiles.
          lhsT_tiles.append(lhsT_tile)

        # Do matmul with all tiles in the blocks
        for bn in range(TILES_IN_BLOCK_N):
          for bm in range(TILES_IN_BLOCK_M):
            # Allocate result_tile in PSUM (uninitialized)
            result_tile = nl.ndarray(shape=(TILE_M, TILE_N),
                                     dtype=nl.float32,
                                     buffer=nl.psum)
            for bk in range(TILES_IN_BLOCK_K):
              # Perform matrix multiply on a tile.
              nisa.nc_matmul(
                dst=result_tile,
                stationary=lhsT_tiles[bk][0:TILE_K, bm * TILE_M:(bm + 1) * TILE_M],
                moving=rhs_tiles[bk][0:TILE_K, bn * TILE_N:(bn + 1) * TILE_N]
              )
            # Accumulate the result into the result_tmps tile.
            nisa.tensor_tensor(dst=result_tmps[m][bm][bn],
                               data1=result_tmps[m][bm][bn],
                               data2=result_tile,
                               op=nl.add)

    # Copying the result from SBUF to HBM
    for m in range(NUM_BLOCK_M):
      for bm in range(TILES_IN_BLOCK_M):
        # coalesce result tiles for better DMA performance
        result_packed = nl.ndarray(shape=(TILE_M, BLOCK_N),
                                   dtype=nl.float32,
                                   buffer=nl.sbuf)
        for bn in range(TILES_IN_BLOCK_N):
          nisa.tensor_copy(
            dst=result_packed[0:TILE_M, bn * TILE_N:(bn + 1) * TILE_N],
            src=result_tmps[m][bm][bn][0:TILE_M, 0:TILE_N])

        # Copy packed result from SBUF to HBM.
        nisa.dma_copy(dst=result[(TILES_IN_BLOCK_M * m + bm) *
                                 TILE_M:(TILES_IN_BLOCK_M * m + bm + 1) * TILE_M,
                                 BLOCK_N * n:BLOCK_N * (n + 1)],
                      src=result_packed[0:TILE_M, 0:BLOCK_N])

  return result
```


## Testing Correctness and Benchmarking

To test the correctness of the kernels, we compare the result with the
`torch.matmul` with `torch.allclose`.


```python
# Test the large workload with tiled kernels
lhs = torch.rand((4096, 1024), dtype=torch.bfloat16, device=device)
rhs = torch.rand((1024, 2048), dtype=torch.bfloat16, device=device)

# Run torch reference
output_torch = torch.matmul(lhs, rhs).to(device=cpu)

def check_match(nki_func):
  output = nki_func(lhs.T, rhs)
  output_nki = output.to(device=cpu)
  if torch.allclose(output_torch, output_nki, atol=1e-4, rtol=1e-2):
    print("NKI and Torch match")
  else:
    print("NKI and Torch differ")

print("Checking correctness of nki_matmul_tiled")
check_match(nki_matmul_tiled_)

print("Checking correctness of nki_matmul_hoist_load")
check_match(nki_matmul_hoist_load_)

print("Checking correctness of nki_matmul_block_free_dimension")
check_match(nki_matmul_block_free_dimension_)

print("Checking correctness of nki_matmul_fully_optimized")
check_match(nki_matmul_fully_optimized_)
```


Output from the test:


```python
Checking correctness of nki_matmul_tiled
NKI and Torch match
Checking correctness of nki_matmul_hoist_load
NKI and Torch match
Checking correctness of nki_matmul_block_free_dimension
NKI and Torch match
Checking correctness of nki_matmul_fully_optimized
NKI and Torch match
```


## Download All Source Code

Click the links to download source code of the kernels and the testing code
discussed in this tutorial.

* All matrix multiplication NKI kernels: [`matrix_multiplication_nki_kernels.py`](../../downloads/matrix_multiplication_nki_kernels.py)

* PyTorch implementation: [`matrix_multiplication_torch.py`](../../downloads/matrix_multiplication_torch.py)

You can also view the source code in the GitHub repository [nki_samples](https://github.com/aws-neuron/nki-samples/tree/main/src/nki_samples/tutorials/matrix_multiplication/)

### Example usage of the scripts:

Run benchmarking of different NKI kernels:


```python
python3 matrix_multiplication_nki_kernels.py
```


Run PyTorch implementation to validate the NKI results against the PyTorch
implementation:


```python
python3 matrix_multiplication_torch.py
```