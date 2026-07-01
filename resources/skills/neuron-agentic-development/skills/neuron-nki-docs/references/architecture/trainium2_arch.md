# Trainium2 Architecture Guide for NKI

Trainium2 Architecture Guide for NKI
In this guide, we will dive into hardware architecture of third-generation NeuronDevices: Trainium2. This guide will highlight major architectural updates compared to the previous generation. Therefore, we assume readers have gone through [Trainium/Inferentia2 Architecture Guide](trainium_inferentia2_arch.md) in detail to understand the basics of NeuronDevice Architecture.

The diagram below shows a block diagram of a Trainium2 device, which consists of:

* 8 NeuronCores (v3).

* 4 HBM stacks with a total device memory capacity of 96GiB and bandwidth of 3TB/s.

* 128 DMA (Direct Memory Access) engines to move data within and across devices.

* 20 CC-Cores for collective communication.

* 4 NeuronLink-v3 for device-to-device collective communication.

!
> **Figure: neuron device3**
>
> An architecture diagram of Trainium2 showing 8 NeuronCore-v3 units arranged in a 4x2 grid, with 128 DMA engines, 20 CC-Cores, HBM memory on all sides, and 4 NeuronLink-v3 interconnects.
>
> This diagram illustrates the architecture of the Trainium2 chip, AWS's second-generation training accelerator.
>
> **Title and layout**:
> - "Trainium2" label in large gray text at top left
> - The chip is shown as a large rounded rectangle
>
> **NeuronCore arrangement**:
> - 8 "NeuronCore-v3" units arranged in a 4-row by 2-column grid
> - Each NeuronCore-v3 is shown as a white rounded rectangle
> - The cores occupy the central portion of the chip
>
> **Memory (HBM)**:
> - Four "HBM" blocks (blue vertical bars) positioned on all sides:
>   - Two on the left side (serving rows 1-2 and rows 3-4)
>   - Two on the right side (same arrangement)
> - This provides high bandwidth memory access to all cores
>
> **Support components** (bottom area):
> - "DMA (x128)": 128 DMA engines shown as stacked gray blocks
> - "CC-Core (x20)": 20 Collective Communication cores shown as stacked gray blocks
> - "Host PCIe": Host interface block on the right
>
> **Interconnects** (bottom):
> - Four "NeuronLink-v3" blocks spanning the bottom, providing inter-chip communication
>
> The design shows significant scaling from Trainium (2 cores) to Trainium2 (8 cores), with proportionally increased DMA engines (32 to 128), CC-Cores (6 to 20), and NeuronLinks (4 NeuronLink-v2 to 4 NeuronLink-v3).
>
> **Key Elements:**
> - **Trainium2**: Second-generation training chip
> - **NeuronCore-v3**: 8 next-generation compute cores (4x2 grid)
> - **HBM**: 4 High Bandwidth Memory blocks (2 left, 2 right)
> - **DMA (x128)**: 128 DMA engines for data movement
> - **CC-Core (x20)**: 20 Collective Communication cores
> - **Host PCIe**: Host interface
> - **NeuronLink-v3**: 4 inter-chip interconnects

Trainium2 Device Diagram.

For a high-level architecture specification comparison from Trainium1 to Trainium2, check out the
[Neuron architecture guide for Trainium2](../../../about-neuron/arch/neuron-hardware/trainium2.md). The rest of this guide will provide details on new features or improvements in NeuronCore-v3 compute engines and memory subsystem compared to NeuronCore-v2.

## NeuronCore-v3 Compute Engine Updates

The figure below is a simplified NeuronCore-v3 diagram of the compute engines and their connectivity to the two on-chip SRAMs, SBUF and PSUM. This is similar to NeuronCore-v2.

!
> **Figure: nki trn2 arch 1**
>
> A NeuronCore architecture diagram showing the internal components including SBUF, four compute engines (Tensor, Vector, Scalar, GPSIMD), PSUM, and Sync Engine, with HBM external memory.
>
> This diagram illustrates the internal architecture of a NeuronCore, showing the memory hierarchy and compute engines.
>
> **Main container** (labeled "NeuronCore" at top):
>
> **SBUF (State Buffer)** - left side:
> - Large blue block representing the main on-chip SRAM
> - Bidirectional arrows connect to HBM below and all compute engines
>
> **Compute Engines** - center column (from top to bottom):
> - **Tensor Engine** (green): Matrix multiplication unit with "SEQ" block to its left
> - **Vector Engine** (green): Vector operations unit with "SEQ" block to its left
> - **Scalar Engine** (green): Scalar operations unit with "SEQ" block to its left
> - **GPSIMD Engine** (green): General-purpose SIMD unit with "SEQ" block to its left
>
> Each engine has bidirectional arrows connecting to SBUF and receives sequencing from SEQ blocks.
>
> **PSUM (Partial Sum)** - top right:
> - Blue block for accumulating matrix multiplication results
> - Connected to Tensor Engine with bidirectional arrows
> - Also connected to Vector Engine
>
> **Sync Engine** - bottom right:
> - Green block for synchronization operations
> - Positioned near GPSIMD Engine
>
> **HBM** - bottom:
> - Large blue block representing off-chip High Bandwidth Memory
> - Bidirectional arrow connects to SBUF
>
> **Data flow**:
> - SBUF serves as the central hub connecting HBM to all compute engines
> - PSUM provides fast accumulation for Tensor Engine operations
>
> **Key Elements:**
> - **NeuronCore**: Main compute unit container
> - **SBUF**: State Buffer - main on-chip SRAM (blue)
> - **PSUM**: Partial Sum accumulator (blue)
> - **Tensor Engine**: Matrix multiplication (green)
> - **Vector Engine**: Vector operations (green)
> - **Scalar Engine**: Scalar operations (green)
> - **GPSIMD Engine**: General-purpose SIMD (green)
> - **Sync Engine**: Synchronization unit (green)
> - **SEQ blocks**: Sequencers for each engine
> - **HBM**: High Bandwidth Memory - external (blue)

NeuronCore-v3 SBUF capacity is **28MiB** (or, 128 partitions of 224KiB), up from 24 MiB in NeuronCore-v2. PSUM capacity remains the same at 2MiB. Engine data-path width and frequency are updated to the following:


| Device Architecture | Compute Engine | Data-path Width (elements/cycle) | Frequency (GHz) |
| --- | --- | --- | --- |
| Trainium2 | Tensor | 4x128 (dense FP8_E4/FP8_E5 input), 2x128 (dense BF16/FP16 input) or 5x128 (sparse input); 1x128 (output) | 2.4 |
|  | Vector | 512 BF16/FP16 input/output; 256 input/output for other data types | 0.96 |
|  | Scalar | 128 input/output | 1.2 |
|  | GpSimd |  | 1.2 |

Next, we will go over major updates to each compute engine.

## Tensor Engine

The Tensor Engine is optimized for tensor computations such as GEMM, CONV, and Transpose. A NeuronCore-v3 Tensor Engine delivers 158 FP8, 79 BF16/FP16/TF32 and 20 FP32 dense TFLOPS of tensor computations. It also delivers 316 FP8/BF16/FP16/TF32 sparse TFLOPS. The rest of this section describes new architectural features introduced in NeuronCore-v3 Tensor Engine.

### Double FP8 Matmul Performance

NeuronCore-v3 TensorEngine (TensorE from now on) supports matrix multiplications (matmuls) of FP8 input matrices (including FP8_E4 and FP8_E5 formats [[1]](#id2)) at **double** the throughput compared to BF16/FP16. Mixing FP8_E4 in one input matrix and FP8_E5 in the other is also allowed. This FP8 double performance mode uses FP32 as the accumulation data type, similar to BF16/FP16 matmul.

[[1](#id1)]
FP8_E3 format is still supported by NeuronCore-v3 TensorE similar to NeuronCore-v2, but its matmul performance is the same as BF16/FP16.

Logically, TensorE doubles the FP8 matmul performance by doubling the maximum contraction dimension of a matmul instruction from 128 (for BF16/FP16) to 256, effectively presenting a 256x128 systolic array to the programmer. Under the hood, since the systolic array is still organized as a grid of 128x128 processing elements, each processing element performs two pairs of FP8 multiplications and also accumulation of the two multiplication results per cycle. The remaining section discusses the semantics of a single double-FP8 matmul instruction. Multiple such instructions can be used to accommodate larger matrix multiplications than the allowed instruction-level tile sizes.

A double-FP8 matmul can perform a multiplication of a 128x256 matrix and a 256x512 matrix (that is, MxKxN matmul, M=128, K=256, N=512). The figure below shows a visualization of the two input matrices (x and y) and the matmul output matrix (output). The figure also highlights two elements (red and yellow) in the first row of the x matrix and in the first column of the y matrix. These two elements are 128 (K//2) elements apart within the rows and columns. We will use these elements to illustrate the SBUF layout requirements for these matrices next.

!
> **Figure: nki trn2 arch 2**
>
> A mathematical view of matrix multiplication showing three matrices (x, y, and output) with specific dimensions (M=128, K=256, N=512), with highlighted elements showing the computation pattern.
>
> This diagram shows the mathematical view of a matrix multiplication operation with specific dimensions, illustrating how individual elements are computed.
>
> **Matrix layout**:
>
> **Top matrix - "y" (blue)**:
> - Dimensions: N=512 (width) by K=256 (height)
> - Label "y" in the center
> - A small yellow/orange square marker on the left edge indicates a specific element
> - A horizontal dashed line passes through the matrix
>
> **Bottom left matrix - "x" (green)**:
> - Dimensions: K=256 (width) by M=128 (height)
> - Label "x" in the center
> - A small red/pink square marker on the top edge
> - A vertical dashed line passes through the matrix
>
> **Bottom right matrix - "output" (purple)**:
> - Dimensions: N=512 (width) by M=128 (height)
> - Label "output" in the center
>
> **Dimension annotations**:
> - "N=512" at the top (width of y and output)
> - "K=256" on the right side of y (height of y, width of x)
> - "M=128" on the left (height of x and output)
> - "N=512" at the bottom of output
> - "K=256" at the bottom of x
>
> **Caption**: "Mathematical View" centered below the diagram
>
> The highlighted markers and dashed lines illustrate how a single element of the output matrix is computed by taking the dot product of a row from x and a column from y.
>
> **Key Elements:**
> - **x matrix**: Green input matrix [M=128 x K=256]
> - **y matrix**: Blue input matrix [K=256 x N=512]
> - **output matrix**: Purple result matrix [M=128 x N=512]
> - **M=128**: Output rows / x rows
> - **K=256**: Contraction dimension (x cols / y rows)
> - **N=512**: Output cols / y cols
> - **Highlighted elements**: Small colored squares showing element correspondence
> - **Dashed lines**: Show alignment for dot product computation
> - **Mathematical View**: Label indicating this is the abstract math perspective

These tensors must still fit in the 128-partition SBUF, with each partition feeding data into each row of processing elements inside the TensorE. The contraction of size 256 is therefore split into two dimensions: (1) the partition dimension of size 128 and (2) the most major (slowest) free dimension of size 2. This is illustrated in the figure below. Both the stationary matrix (x in above figure) and the moving matrix (y in above figure) are sliced in two tiles, where the first and second tile correspond to first and second halves of the contraction dimension, respectively.

!
> **Figure: nki trn2 arch 3**
>
> A diagram showing the tensor layout in SBUF for matrix multiplication, with stationary matrix (green) and moving matrix (blue) both tiled with K/2=128 partition dimension.
>
> This diagram illustrates how the input matrices for matrix multiplication are laid out in the State Buffer (SBUF), showing the tiling along the partition dimension.
>
> **Left tensor - "stationary (SBUF)" (green)**:
> - Green rectangular block
> - Dimensions: M=128+M=128 (width, showing two M tiles) by K/2=128 (height)
> - Small colored markers (red and yellow squares) at the top corners
> - A vertical dashed line divides the tensor into two M=128 tiles
> - Label "stationary (SBUF)" in center
>
> **Right tensor - "moving (SBUF)" (blue)**:
> - Blue rectangular block  
> - Dimensions: N=512+N=512 (width, showing two N tiles) by K/2=128 (height)
> - Small colored markers at the top
> - A vertical dashed line divides the tensor into two N=512 tiles
> - Label "moving (SBUF)" in center
>
> **Dimension annotations**:
> - "K/2 =128" on the left side of both tensors (partition dimension)
> - "M=128" twice at bottom of stationary tensor
> - "N=512" twice at bottom of moving tensor
> - "SBUF P-dim" label on right with a vertical arrow indicating the partition dimension orientation
>
> **Caption**: "Tensor Layout in SBUF" centered below
>
> The diagram shows that:
> - Both matrices have their K dimension (256 total) tiled into K/2=128 chunks along the SBUF partition dimension
> - The stationary matrix has its M dimension in the free dimension
> - The moving matrix has its N dimension in the free dimension
>
> **Key Elements:**
> - **stationary (SBUF)**: Green tensor loaded for Tensor Engine [K/2=128 x M=256]
> - **moving (SBUF)**: Blue tensor that streams through [K/2=128 x N=1024]
> - **K/2=128**: Half of K dimension per tile (partition dimension)
> - **M=128**: Free dimension size per tile for stationary
> - **N=512**: Free dimension size per tile for moving
> - **SBUF P-dim**: Partition dimension indicator
> - **Vertical dashed lines**: Tile boundaries
> - **Colored markers**: Position reference points

Next, we invoke the LoadStationary and MultiplyMoving instructions to perform the matrix multiplications using the above tensors in SBUF. This is illustrated in figure below. The LoadStationary instruction loads the stationary tensor (K/2=128, 2, M=128) into TensorE, which stores two data elements into a single processing element (for example, the red and yellow elements land in the first processing element of TensorE as shown in ❶). Next, the MultiplyMoving instruction streams the moving tensor horizontally across the loaded stationary tensor. Similar to LoadStationary, two elements of moving tensor are sent to the same processing element simultaneously as shown in ❷, such that they can get multiplied with the corresponding pair of loaded stationary elements.

!
> **Figure: nki trn2 arch 4**
>
> A two-part diagram showing Tensor Engine operations: (a) LoadStationary instruction loading data from SBUF to TensorE, and (b) MultiplyMoving instruction showing the full matmul operation with output to PSUM.
>
> This diagram illustrates the two key Tensor Engine instructions for matrix multiplication.
>
> **Part (a) - LoadStationary Instruction** (top):
> - Shows data movement from SBUF to Tensor Engine
> - Left side: "stationary (TensorE)" green block with dimensions K/2=128 (height)
> - Right side: "stationary (SBUF)" green block with dimensions K/2=128 (height) and M=128 twice (width, divided by dashed line)
> - A black arrow labeled "1" curves from SBUF to TensorE, indicating the load operation
> - Small colored markers (red, yellow) at tile boundaries
> - "SBUF P-dim" label on right
>
> **Part (b) - MultiplyMoving Instruction** (bottom):
> - Shows the multiplication operation with moving matrix
> - Left side: "stationary (TensorE)" green block, K/2=128 height
> - Center-right: "moving (SBUF)" blue block with K/2=128 height and N=512 twice width
> - Arrow labeled "2" curves from moving tensor to stationary, indicating the multiply operation
> - Below: "output (PSUM)" purple block with dimensions 512 (height) and 128 (width)
> - Arrow from TensorE down to PSUM
> - "SBUF P-dim" label on right
> - "PSUM P-dim" label below output
>
> **Dimension annotations**:
> - K/2=128: Partition dimension for both operations
> - M=128: Free dimension tiles for stationary
> - N=512: Free dimension tiles for moving
> - 512, 128: Output dimensions in PSUM
>
> **Key Elements:**
> - **LoadStationary Instruction (a)**: Load stationary matrix from SBUF to Tensor Engine
> - **MultiplyMoving Instruction (b)**: Multiply with moving matrix, store in PSUM
> - **stationary (TensorE)**: Matrix held in Tensor Engine (green)
> - **stationary (SBUF)**: Source for stationary matrix (green)
> - **moving (SBUF)**: Moving matrix that streams through (blue)
> - **output (PSUM)**: Result in Partial Sum buffer (purple)
> - **Numbered arrows (1, 2)**: Operation sequence
> - **SBUF P-dim, PSUM P-dim**: Partition dimension labels

Note that the above double FP8 `LoadStationary`/`MultiplyMoving` instruction sequence with a 256 contraction dimension takes the same amount of time as the regular BF16/FP16 LoadStationary/MultiplyMoving instruction sequence with a 128 contraction dimension. Since the double FP8 instruction performs double the FLOPs, overall double FP8 matmul on TensorE can achieve double the throughput compared to BF16/FP16 matmuls.

NKI programmers can invoke double FP8 matmul using the `nisa.nc_matmul()` API on NeuronCore-v3:


```python
import nki.isa as nisa

# stationary: [128, 2, 128]
# moving: [128, 2, 512]
# dst: [128, 512]
nisa.nc_matmul(dst, stationary, moving,
               perf_mode=nisa.matmul_perf_mode.double_row, ...)
```


The `nt.tensor[128, 2, 128]` stationary and `nt.tensor[128, 2, 512]` moving tensor shapes reflect the maximum tile sizes for the double FP8 matmul instruction. Smaller tile sizes are supported, though the second dimension (the most major free dimension) of both input tensors must be two. In other words, if the contraction dimension of the matmul is not a multiple of two, programmers are required to explicitly pad the input tensors with zeros to enable the performance mode.

A full NKI kernel example performing double FP8 matmul is available on [Github](https://github.com/aws-neuron/nki-samples/blob/main/src/nki_samples/reference/double_row_matmul.py).

Note that Double FP8 matmul performance mode cannot be combined with the following TensorE features:

* Column tiling mode

* Sparse matmul (new in NeuronCore-v3, discussion below)

* Transpose mode (new in NeuronCore-v3, more discussion below)

### Built-in Transpose Support

As discussed in [Trainium/Inferentia2 Architecture Guide](trainium_inferentia2_arch.md), one common use of TensorE besides matrix multiplication operations is transposition of a 2D SBUF tensor, which swaps the partition and free dimension of the matrix. Such a transposition is done through a matmul of the tensor to be transposed (stationary tensor) and an identity matrix (moving tensor). Prior to NeuronCore-v3, TensorE has to perform multiplication of each data element with 1.0 or 0.0 and accumulation along the contraction dimension normally. However, if the tensor to be transposed contains NaN/Inf floating point values, the matmul result will not be a bit-accurate transposition of the original matrix - the NaN/Inf values will propagate through the accumulation chain and spread across the output tensor.

Starting with NeuronCore-v3, TensorE supports an explicit transpose mode, which can correctly transpose input tensors with NaN/Inf. In addition, the transpose mode provides the following benefits:

* 2x speedup in FP32 transpose, vs. no transpose mode enabled.

* FP16/BF16 PSUM output for FP16/BF16 transpose, vs. FP32 (default matmul output data type) PSUM output when no transpose mode enabled. This allows faster PSUM data eviction back to SBUF.

> **Note**
>
> Note
> 
> 
> NeuronCore-v3 TensorE transpose mode for FP8 input data produces 16-bit output elements in PSUM, with the upper 8 bits filled with zeros.

NKI programmers can enable TensorE transpose mode on NeuronCore-v3 through the following APIs:


```python
nisa.nc_matmul(..., is_transpose=True)
# OR
nisa.nc_transpose(..., engine=nisa.constants.engine.tensor)
```


## Vector Engine

Vector Engine (VectorE) is specially designed to accelerate vector operations where every element in the output tensor typically depends on multiple elements from input tensor(s), such as vector reduction and element-wise operators between two tensors. NeuronCore-v3 Vector Engine delivers a total of 1.0 TFLOPS of FP32 computations and can handle various input/output data-types, including FP8, FP16, BF16, TF32, FP32, INT8, INT16, and INT32.

### Vector Engine Performance Mode

NeuronCore-v3 Vector Engine provides a new performance mode BF16/FP16 data types, which quadruples or doubles the instruction throughput depending on the instruction type compared to NeuronCore-v2 (more details below). Enabling this performance mode does not change the computation precision - all computation is still done in FP32, similar to NeuronCore-v2 Vector Engine.

In particular, the following instructions could see a 4x throughput lift compared to NeuronCore-v2:

* 
`nisa.tensor_copy` and `nisa.tensor_scalar` when both input/output tensors:

are in SBUF

* are in BF16/FP16 (input and output data types do not need to match)

* have physically contiguous elements in the inner-most (most minor) free dimension

The following instructions could see a 2x throughput lift compared to NeuronCore-v2:

* 
`nisa.tensor_copy` and `nisa.tensor_scalar`:

when both input/output tensors satisfy 1a and 1b, but not 1c conditions above, or

* when both input/output tensors satisfy 1b and 1c, but one of input and output tensors is in PSUM

* 
`nisa.tensor_tensor`:

when both input tensors are SBUF and all of input/output tensors are in BF16/FP16

Note, NKI programmers are not required to explicitly enable VectorE performance mode. VectorE detects the above conditions and enables performance mode automatically in hardware.

## Scalar Engine

As discussed in Trainium/Inferentia2 Architecture Guide, Scalar Engine (ScalarE) is specially designed to accelerate scalar operations where every element in the output tensor only depends on one element of the input tensor. In addition, ScalarE provides hardware acceleration to evaluate non-linear functions such as Gelu and Sqrt. All architectural capabilities from NeuronCore-v2 Scalar Engine are applicable to NeuronCore-v3. NeuronCore-v3 Scalar Engine additionally supports bit-accurate tensor copies without intermediate FP32 data type casting, similar to VectorE and Gpsimd Engine (see details in `nisa.tensor_copy`).

## Gpsimd Engine

GpSimd Engine (GpSimdE) is intended to be a general-purpose engine that can run any ML operators that cannot be lowered onto the other highly specialized compute engines discussed above efficiently, such as applying a triangular mask to a tensor. A GpSimdE consists of eight fully programmable processors that can execute arbitrary C/C++ programs.

In NeuronCore-v3, each processor in GpsimdE also comes with an integrated DMA engine that can move data in parallel to computation on GpsimdE and also parallel to data movements done by the main DMA engines on the Neuron Device. These integrated DMA engines can reach any SBUF/HBM on-chip or off-chip in the same trn2 instance. All eight processors together have a total integrated DMA bandwidth of 307 GB/s (153 GB/s per read/write direction).

In NeuronCore-v3, each processor in GpsimdE also comes with an integrated DMA engine that can move data in parallel to computation on GpsimdE and also parallel to data movements done by the main DMA engines on the Neuron Device. These integrated DMA engines can reach any SBUF/HBM on-chip or off-chip in the same trn2 instance. All eight processors together have a total integrated DMA bandwidth of 307 GB/s (153 GB/s per read/write direction).

## Data Movement Updates

Trainium2 consists of a three-tiered memory hierarchy: HBM, SBUF and PSUM, from highest to lowest memory capacity. Figures below show the specifications of these memories and their connectivity for one NeuronCore-v3.

!
> **Figure: nki trn2 arch 5 1**
>
> A memory hierarchy pyramid diagram showing four levels from Host CPU memory at the bottom to PSUM at the top, with capacity and bandwidth specifications for each level and data flow operations labeled.
>
> This diagram illustrates the complete memory hierarchy for NeuronCore systems as a pyramid, with faster/smaller memory at the top and slower/larger memory at the bottom.
>
> **Pyramid levels (top to bottom)**:
>
> **Level 1 - PSUM (top, yellow)**:
> - Capacity: ~2 MB
> - Bandwidth: ~10 TB/sec
> - Blue arrow up: "MatMult" (data flows up from SBUF for matrix multiplication)
> - Red arrow down: "Use MatMult result" (results flow back to SBUF)
>
> **Level 2 - SBUF (yellow/green)**:
> - Capacity: ~25 MB
> - Bandwidth: ~10 TB/sec
> - Central position in the on-chip hierarchy
>
> **Level 3 - Device memory (HBM) (green)**:
> - Capacity: ~50 GB
> - Bandwidth: ~0.5 TB/sec per NC (NeuronCore)
> - Blue arrow up: "Refill, or Start NKI kernel"
> - Red arrow down: "Spill, or End NKI kernel"
>
> **Level 4 - Host (CPU) memory (DRAM) (blue/gray)**:
> - Capacity: ~1 TB
> - Bandwidth: ~16 GB/sec
> - Blue arrow up: "Start compute graph"
> - Red arrow down: "End compute graph"
>
> **Right side annotations**:
> - Bracket labeled "Memory within NeuronCore (on-chip)" encompasses PSUM and SBUF
> - Bracket labeled "Memory within Neuron Device" encompasses PSUM, SBUF, and HBM
>
> **Color coding for arrows**:
> - Blue arrows: Data moving up the hierarchy (toward compute)
> - Red arrows: Data moving down the hierarchy (results/spill)
>
> **Key Elements:**
> - **PSUM**: ~2 MB, ~10 TB/sec - fastest, smallest (top)
> - **SBUF**: ~25 MB, ~10 TB/sec - main on-chip buffer
> - **Device memory (HBM)**: ~50 GB, ~0.5 TB/sec per NC
> - **Host (CPU) memory (DRAM)**: ~1 TB, ~16 GB/sec - largest, slowest
> - **MatMult / Use MatMult result**: PSUM operations
> - **Refill / Spill**: HBM to SBUF operations
> - **Start/End NKI kernel**: HBM operations
> - **Start/End compute graph**: Host memory operations
> - **On-chip vs Device memory**: Hierarchy classification

!
> **Figure: nki trn2 arch 6**
>
> A NeuronCore memory hierarchy diagram showing the relationship between on-chip components (PSUM, compute engines, SBUF) and off-chip HBM, with DMA engines facilitating data transfer.
>
> This diagram illustrates the memory hierarchy within a NeuronCore, organized vertically with on-chip components at the top and off-chip memory at the bottom.
>
> **On-chip section** (enclosed in dashed rectangle on right):
>
> **PSUM (top)** - peach/orange colored block:
> - Spans full width
> - Partial Sum accumulator for Tensor Engine outputs
> - Bidirectional arrows connect to compute engines below
>
> **Compute Engines** - four blocks in a row:
> - **TensorE**: Tensor Engine (leftmost)
> - **VectorE**: Vector Engine
> - **ScalarE**: Scalar Engine
> - **GpSimdE**: General Purpose SIMD Engine (rightmost)
> - Each has bidirectional arrows to PSUM above and SBUF below
>
> **SBUF (middle)** - green colored block:
> - State Buffer - main on-chip SRAM
> - Spans full width
> - Central hub connecting compute engines to external memory
>
> **DMA Engines** - multiple blocks below SBUF:
> - Four "DMA" blocks shown with "..." indicating more
> - Bidirectional arrows connect to SBUF above and HBM below
> - Facilitate data movement between on-chip and off-chip memory
>
> **Off-chip section**:
>
> **HBM (bottom)** - light blue colored block:
> - High Bandwidth Memory
> - External device memory
> - Connected to DMA engines above
>
> **Labels**:
> - "on-chip" bracket on right encompassing PSUM through DMA
> - "off-chip" bracket on right for HBM
> - Dashed line separates on-chip from off-chip
>
> **Key Elements:**
> - **PSUM**: Partial Sum buffer (peach/orange)
> - **TensorE, VectorE, ScalarE, GpSimdE**: Four compute engines
> - **SBUF**: State Buffer - main on-chip SRAM (green)
> - **DMA**: Multiple DMA engines for data movement
> - **HBM**: High Bandwidth Memory - off-chip (light blue)
> - **on-chip / off-chip labels**: Memory hierarchy classification
> - **Bidirectional arrows**: Data flow paths
> - **Dashed rectangle**: On-chip boundary

As shown in the above figures, data movement between HBM and SBUF is performed using on-chip DMA (Direct Memory Access) engines, which can run in parallel to computation within the NeuronCore. Data movement between PSUM and SBUF is done through ISA instructions on the compute engines. In NeuronCore-v3, two restrictions in engine parallel accesses to SBUF/PSUM are lifted to improve programming flexibility compared to NeuronCore-v2:

* 
VectorE and GpSimdE can access SBUF in parallel.

This was disallowed in NeuronCore-v2.

* VectorE’s performance mode leverages a shared memory bus between the VectorE and GpsimdE engines to deliver 2-4x performance improvement for select VectorE instructions. The hardware automatically coordinates access between engines to optimize bus utilization, including arbitrating between GpsimdE and relevant VectorE instructions.

* 
VectorE and ScalarE can access PSUM in parallel.

This was disallowed in NeuronCore-v2.

* Both VectorE and ScalarE can access PSUM at full bandwidth in parallel, as long as their accesses do not collide on the same PSUM bank.

### DMA Transpose

Trainium2 DMA engines can perform a tensor transpose while moving data from HBM into SBUF, or from SBUF to SBUF itself. The figure below illustrates these two supported DMA transpose data flows. Trainium2 DMA transpose supports bit-accurate transposition for both 2-byte and 4-byte data types.

!
> **Figure: nki trn2 arch 7**
>
> A diagram showing the DMA transpose mechanism where data flows from HBM through multiple DMA engines and an Xpose Block to produce both transposed (data.T) and non-transposed (data) outputs in SBUF.
>
> This diagram illustrates the hardware transpose capability during DMA transfers from HBM to SBUF.
>
> **Left side - HBM (gray)**:
> - Large gray block representing High Bandwidth Memory
> - Contains "data" label indicating source data
> - Arrow points right showing data flow
>
> **Center - DMA and Xpose Block**:
> - Multiple "DMA" blocks stacked vertically (4 shown with "..." indicating more)
> - Arrows flow from HBM through DMA blocks
> - All DMA outputs feed into a central "Xpose Block" (purple)
> - Numbers "(1)" and "(2)" in purple indicate two output paths from the Xpose Block
>
> **Right side - SBUF (green)**:
> - Large green block representing State Buffer
> - Two outputs from Xpose Block:
>   - "data.T" (transposed data) - upper path
>   - "data" (original layout) - lower path
> - Both paths merge into SBUF
>
> The Xpose Block is the hardware unit that performs on-the-fly transpose during DMA operations, allowing data to be written to SBUF in either transposed or non-transposed format without additional compute operations.
>
> **Key Elements:**
> - **HBM**: Source High Bandwidth Memory (gray)
> - **data**: Source data in HBM
> - **DMA**: Multiple DMA engines (stacked blocks)
> - **Xpose Block**: Hardware transpose unit (purple)
> - **(1), (2)**: Two output paths from transpose block
> - **data.T**: Transposed output to SBUF
> - **data**: Non-transposed output to SBUF
> - **SBUF**: Destination State Buffer (green)
> - **Ellipsis (...)**: Indicates additional DMA engines


#### HBM2SBUF DMA transpose

Before diving into how HBM2SBUF transpose works, let’s revisit a simple DMA copy from a packed HBM tensor `[128, 512]` to an SBUF tensor `[nl.par_dim(128), 512]`. Following Numpy convention, these tensor shapes follow a major to minor ordering. The figure below visualizes these HBM and SBUF tensors. A packed `[128, 512]` HBM tensor consists of 128 chunks of 512 elements, laid out back to back in the HBM linear memory. The most minor (that is, inner-most) dimension consists of 512 contiguous elements in memory. Once loaded into the SBUF, the most minor HBM tensor dimension (512) is mapped to the free dimension of the SBUF, while the most major dimension is mapped to the SBUF partition dimension.

In Trainium2, each NeuronCore-v3 is typically paired with 16x DMA engines to drive its corresponding SBUF bandwidth. In the above DMA copy, each DMA engine would be responsible for moving 128/16 = 8 chunks of 512 elements.

* HBM tensor [128, 512]: 512 is the inner-most (minor) dimension

!
> **Figure: nki trn2 arch 8**
>
> A diagram showing DMA copy operation from HBM tensor [128, 512] to SBUF tensor with partition dimension, where contiguous 512-element rows become columns in the SBUF 2D layout.
>
> This diagram illustrates how a DMA copy operation maps a linear HBM tensor to a 2D SBUF tensor layout.
>
> **Left side - Source HBM tensor**:
> - Horizontal strip showing "src: HBM tensor [128, 512]"
> - Three colored blocks (blue, purple, green) each representing 512 elements
> - Dimensions labeled as "512", "512", "512" above each block
> - Total size annotation: "128 x 512"
> - Ellipsis (...) indicates additional rows
>
> **Center - Arrow**:
> - "DMA copy" label with arrow pointing right
> - Indicates the data movement operation
>
> **Right side - Destination SBUF tensor**:
> - Large rectangular block showing "dst: SBUF tensor [nl.par_dim(128), 512]"
> - Dimensions: "512 F" (free dimension) width, "128 P" (partition dimension) height
> - Same colored blocks (blue, purple, green) now arranged vertically as rows
> - Each block occupies one partition row
> - "SBUF P-dim" label on right with vertical arrow
> - Ellipsis (...) indicates additional partitions
>
> The key transformation:
> - HBM tensor is logically [128, 512] (128 rows of 512 elements each)
> - In SBUF, these become 128 partitions (P-dim) with 512 elements (F-dim) each
> - The nl.par_dim(128) indicates the partition dimension designation
>
> **Key Elements:**
> - **src: HBM tensor [128, 512]**: Source tensor with 128 rows of 512 elements
> - **DMA copy**: Data movement operation
> - **dst: SBUF tensor [nl.par_dim(128), 512]**: Destination with partition dimension
> - **512 F**: Free dimension (512 elements)
> - **128 P**: Partition dimension (128 partitions)
> - **Colored blocks**: Visual tracking of data chunks (blue, purple, green)
> - **SBUF P-dim**: Partition dimension indicator

In contrast, in a DMA transpose operation, we take an HBM tensor of opposite layout [512, 128]:

!
> **Figure: nki trn2 arch 9**
>
> A diagram showing DMA transpose operation from HBM tensor [512, 128] to SBUF tensor, where the data is transposed during transfer so rows become distributed across the free dimension.
>
> This diagram illustrates how a DMA transpose operation maps and transposes a linear HBM tensor to a 2D SBUF tensor layout.
>
> **Left side - Source HBM tensor**:
> - Horizontal strip showing "src: HBM tensor [512, 128]"
> - Three colored blocks (blue, purple, green) each representing 128 elements
> - Dimensions labeled as "128", "128", "128" above each block
> - Total size annotation: "512 x 128"
> - Ellipsis (...) indicates additional rows
>
> **Center - Arrow**:
> - "DMA transpose" label with arrow pointing right
> - Indicates the data movement with transpose operation
>
> **Right side - Destination SBUF tensor**:
> - Large rectangular block showing "dst: SBUF tensor [nl.par_dim(128), 512]"
> - Dimensions: "512 F" (free dimension) width, "128 P" (partition dimension) height
> - Same colored blocks (blue, purple, green) now arranged as vertical columns within the tensor
> - Each original row becomes a column in the transposed layout
> - "SBUF P-dim" label on right with vertical arrow
> - Ellipsis (...) indicates additional columns
>
> The key transformation:
> - HBM tensor is [512, 128] (512 rows of 128 elements)
> - After transpose, becomes [128, 512] in SBUF
> - Original 128-element rows become columns in the free dimension
> - The 512 original rows become distributed across the free dimension
>
> **Key Elements:**
> - **src: HBM tensor [512, 128]**: Source tensor (512 x 128)
> - **DMA transpose**: Data movement with transpose operation
> - **dst: SBUF tensor [nl.par_dim(128), 512]**: Transposed destination
> - **512 F**: Free dimension after transpose
> - **128 P**: Partition dimension after transpose
> - **Colored blocks**: Visual tracking showing transposition (blue, purple, green as columns)
> - **SBUF P-dim**: Partition dimension indicator

In a DMA transposition, the most minor dimension of the source HBM tensor now becomes the partition dimension of the SBUF in destination. Compared to the above DMA copy operation where each DMA engine reads and writes an independent slice of 512 elements, DMA transpose requires all 16x DMA engines to work co-operatively to deliver the best throughput - these 16x DMA engines should write into a single `[nl.par_dim(128), 16]` SBUF tile in parallel at a time, where the 16 elements along free dimension must be contiguous. Having a multiple of 128 and a multiple of 16 in the output SBUF partition and inner-most free dimension sizes is a pre-requisite to achieve best DMA throughput efficiency possible with DMA transpose. However, it is not a functionality requirement - DMA transpose can flexible tile sizes for DMA transpose at the cost of DMA performance.

HBM2SBUF DMA transpose is commonly seen in ML workloads where the data layout in HBM differs from the format needed by the initial compute engine that processes the data. For example, in the LLM decode phase, the K cache typically has an HBM layout of `[seqlen, d_head]`, where `seqlen` and `d_head` are the sequence length and head dimensions respectively. However, when K is consumed by TensorE in the Q&#64;K operator in self-attention, `d_head` is the contraction dimension of the matrix multiplication. Therefore, the most-minor d_head dimension in HBM should become the partition dimension to satisfy TensorE layout requirements (see [Tiling](../programming/tiling-overview.md#nki-tile-layout): Contraction dimension must map to partition dimension). Mapping most minor HBM tensor dimension to SBUF partition dimension is exactly an HBM2SBUF DMA transpose operation on Trainium2.

In NKI, programmers can invoke an HBM2SBUF DMA transpose using the `nisa.dma_transpose` API.


```python
import nki
import nki.language as nl
import nki.isa as nisa

# hbm_src: nt.tensor[512, 128]
# sbuf_dst: nt.tensor[128, 512]
sbuf_dst = nisa.dma_transpose(src=hbm_src)
```


> **Note**
>
> Performance Consideration
> 
> 
> DMA transpose on Trainium2 can achieve up to 90% DMA throughput utilization given hardware-friendly tensor access patterns, compared to up to 100% throughput utilization for a DMA copy.

#### SBUF2SBUF DMA transpose

SBUF2SBUF DMA transpose works in a similar fashion as HBM2SBUF transpose, where the most minor dimension of the input SBUF tensor, i.e., inner-most free dimension, becomes the partition dimension of the output SBUF tensor. Therefore, SBUF2SBUF DMA transpose is a way to swap partition and free axis of an SBUF tensor, an alternative to TensorE transpose.

The same `nisa.dma_transpose` API can be used to perform an SBUF2SBUF DMA transpose:


```python
import nki
import nki.language as nl
import nki.isa as nisa

# sbuf_src: nt.tensor[128, 128]
# sbuf_dst: nt.tensor[128, 128]
sbuf_dst = nisa.dma_transpose(src=hbm_src)
```


Performance Consideration. SBUF2SBUF transpose can achieve up to 50% of DMA throughput on Trainium2. Compared to TensorE transpose that is more performant but requires ScalarE/VectorE to evict the transposed output from PSUM back to SBUF, DMA transpose can read from and write to SBUF directly. Therefore, DMA transpose is particularly useful in operators that are ScalarE/VectorE bound, such as self attention.

### Descriptor Generation Engine (DGE)

The Descriptor Generation Engine (DGE) is a new hardware block in NeuronCore-v3 that accelerates DMA descriptor generation to perform either DMA copy or transpose on the DMA engines. Each NeuronCore-v3 comes with two instances of DGE, which can be commanded through either SyncE or ScalarE sequencer. The figure below shows the connectivity of the DGE instances.

!
> **Figure: nki trn2 arch 10**
>
> A diagram showing DMA engines and DGE (DMA Gather Engine) components interfacing with NeuronCore's Scalar Engine and Sync Engine, illustrating the descriptor-based DMA command architecture.
>
> This diagram illustrates the DMA subsystem architecture and how it interfaces with the NeuronCore for data movement operations.
>
> **Left side - DMA engines**:
> - Four "DMA" blocks shown as stacked gray rectangles (with "..." indicating more)
> - These represent the pool of DMA engines available for data transfers
>
> **Center - DGE (DMA Gather Engines)**:
> - "DGE[0]" (purple block) at top
> - "DGE[1]" (blue block) at bottom
> - Each DGE connects to multiple DMA engines via "desc" (descriptor) arrows
> - The DGEs gather and dispatch DMA operations
>
> **Arrows and connections**:
> - "desc" arrows: From DMA engines to both DGE[0] and DGE[1], showing descriptor-based control
> - Lines cross between DMA engines and DGEs, indicating flexible mapping
> - "cmd" arrows: From DGE[0] and DGE[1] to NeuronCore components
>
> **Right side - NeuronCore**:
> - Rounded rectangle labeled "NeuronCore"
> - Contains two components:
>   - "SEQ" block (gray) with "Scalar Engine" (green) - receives "cmd" from DGE[0]
>   - "Sync Engine" (green) - receives "cmd" from DGE[1]
>
> The diagram shows that:
> 1. DMA engines are controlled via descriptors
> 2. DGEs aggregate DMA commands
> 3. Scalar Engine controls DGE[0] for general data movement
> 4. Sync Engine controls DGE[1] for synchronized transfers
>
> **Key Elements:**
> - **DMA**: Multiple DMA engines for data movement (gray blocks)
> - **DGE[0]**: DMA Gather Engine 0 (purple) - controlled by Scalar Engine
> - **DGE[1]**: DMA Gather Engine 1 (blue) - controlled by Sync Engine
> - **desc**: Descriptor-based DMA control paths
> - **cmd**: Command paths to NeuronCore
> - **NeuronCore**: Target compute unit
> - **SEQ + Scalar Engine**: Sequencer and scalar processing (green)
> - **Sync Engine**: Synchronization control (green)
> - **Ellipsis (...)**: Indicates additional DMA engines

Prior to Trainium2, DMA descriptor generation was handled in two ways. They were either generated statically on the host when loading a NEFF onto a Neuron Device (i.e., static DMA), or created dynamically through custom kernels on GpsimdE during NEFF execution (i.e., software DGE). The static approach stored all descriptors in HBM, consuming valuable memory space that could otherwise be used for model parameters or computation data. The software-based approach used a portion of SBUF for storing descriptors generated during execution and occupies GpsimdE that could otherwise perform useful computation.

In comparison, the new hardware-based DGE in Trainium2 generates descriptors on demand without requiring additional memory storage. It also frees up GpsimdE to perform useful computation. Therefore, it is recommended to leverage hardware-based DGE on Trainium2 whenever possible to initiate a DMA transfer.

NKI programmers can invoke hardware-based DGE on NeuronCore-v3 using `nisa.dma_copy` and `nisa.dma_transpose` APIs, by setting `dge_mode=nisa.dge_mode.hw_dge`. The compute engine to initiate a DGE command (Sync Engine or ScalarE) is currently determined by NKI compiler (subject to changes).

> **Note**
>
> Note
> 
> 
> NeuronCore-v3 hardware DGE currently does not support indirect DMA operations (gather/scatter). Refer to nisa API documentation for detailed implementation guidelines.

> **Note**
>
> Performance Consideration
> 
> 
> When triggered from ScalarE, execution of the DGE-based DMA instruction could be hidden behind earlier compute instructions (such as `nisa.activate()`) in program order, since DGE and the compute pipeline of ScalarE are independent hardware resources. Each DGE-based DMA instruction takes about 600 ns to execute on NeuronCore-v3.