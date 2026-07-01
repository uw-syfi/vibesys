# Trainium/Inferentia2 Architecture Guide for NKI

Trainium/Inferentia2 Architecture Guide for NKI
In this guide, we will dive into hardware architecture of second-generation NeuronDevices: Trainium/Inferentia2.
Our goal is to equip advanced Neuron users with sufficient architectural knowledge to write performant NKI kernels and
troubleshoot performance issues on NeuronDevices using [Neuron Explorer](../optimization/use-neuron-profile.md),
a profiler tool designed specifically for NeuronDevices. This guide is also written assuming readers have read
through [NKI Language Guide](../programming/nki-language-guide.md) and familiarized themselves with key NKI concepts.

[Fig. 47](#fig-arch-neuron-device-v2) shows a block diagram of a Trainium and Inferentia2 device.
At a high level, both Trainium and Inferentia2 devices consist of:

* 2 NeuronCores (v2).

* 2 HBM stacks with a total device memory capacity of 32GiB and bandwidth of 820 GB/s.

* 32 DMA (Direct Memory Access) engines to move data within and across devices.

* 6 CC-Cores for collective communication.

* 2 (Inferentia2) or 4 (Trainium) NeuronLink-v2 for device-to-device collective communication.


> **Figure: neuron device2**
>
> A side-by-side comparison of Trainium and Inferentia2 device architectures, showing both devices share similar components (2 NeuronCore-v2 units, HBM, DMA, CC-Cores) but with different configurations.
>
> This diagram compares the architecture of two AWS Neuron devices side by side.
>
> **Left side - Trainium**:
> - Title "Trainium" in blue at top left
> - Host PCIe interface at top right
> - "32x DMA" and "6x CC-Core" blocks below Host PCIe
> - Two "NeuronCore-v2" units arranged in a 2x1 layout
> - Each NeuronCore-v2 contains:
>   - On-chip SRAM memory (cylinder icons)
>   - Tensor Engine (grid icon)
>   - Vector Engine (wave icon)
>   - Scalar Engine (curve icon)
>   - GPSIMD Engine (grid icon)
> - "HBM" labels on left side (two instances)
> - Four "NeuronLink-v2" blocks at bottom for inter-device communication
>
> **Right side - Inferentia2**:
> - Title "Inferentia2" in blue at top
> - Same basic layout as Trainium
> - Host PCIe at top
> - "32x DMA" and "6x CC-Core" blocks
> - Two "NeuronCore-v2" units with identical internal components:
>   - On-chip SRAM memory
>   - Tensor Engine
>   - Vector Engine
>   - Scalar Engine
>   - GPSIMD Engine
> - "HBM" labels on left side
> - Single "NeuronLink-v2" block at bottom (less interconnect than Trainium)
>
> Both devices share the NeuronCore-v2 architecture but Trainium has more NeuronLink-v2 connections (4 vs 1), reflecting its focus on training workloads requiring more inter-device communication.
>
> **Key Elements:**
> - **Trainium**: Training-focused device (left)
> - **Inferentia2**: Inference-focused device (right)
> - **NeuronCore-v2**: Two compute cores per device
> - **On-chip SRAM memory**: Local storage in each core
> - **Tensor/Vector/Scalar/GPSIMD Engines**: Compute units
> - **32x DMA**: 32 DMA engines for data movement
> - **6x CC-Core**: 6 Collective Communication cores
> - **HBM**: High Bandwidth Memory (2 stacks per device)
> - **NeuronLink-v2**: Inter-device links (4 for Trainium, 1 for Inferentia2)
> - **Host PCIe**: Host interface


Fig. 47 Trainium/Inferentia2 Device Diagrams.

The rest of this guide will go into details of each compute engine in NeuronCore-v2 and supported data movement
patterns across the memory hierarchy.

## NeuronCore-v2 Compute Engines

In this section, we will describe the architectural details within a NeuronCore-v2. The figure below is a simplified diagram
of the compute engines and their connectivity to the two on-chip SRAMs: state buffer (SBUF) and partial sum buffer (PSUM).

[![../../../_images/pm-nc.png](../../../_images/pm-nc.png)](../../../_images/pm-nc.png)

Fig. 48 NeuronCore-v2 and its device memory (HBM).

A NeuronCore-v2 consists of four heterogeneous compute engines (Tensor, Vector, Scalar, and GpSimd), each designed to accelerate different types of operators in modern machine learning models. Each compute engine has its own sequencer, which is responsible for instruction fetch, decode, and issue. The four compute engines execute four independent instruction streams asynchronously in parallel. Explicit synchronization to satisfy data dependencies between engines is handled through atomic semaphores in hardware. In NKI, programmers do not need to program engine synchronization manually. The Neuron Compiler can automatically insert the required synchronizations during compilation, based on data dependencies identified in the NKI kernel.

The instruction stream within each compute engine consists of both control and data-path instructions. Control instructions are executed directly by the engine sequencer and can perform scalar operations using a set of 32-bit scalar registers private to each sequencer. Examples of control instructions include register ALU operations for dynamic condition and address calculations, branching for control flow execution, and triggering DMA transfers. Data path instructions are executed by the specialized engine data path, which interacts with tensors in SBUF/PSUM. Data path instructions can handle flexible addressing and shapes by referencing values stored in scalar registers.

Within each NeuronCore, there is also a Sync Engine, which functions as an engine sequencer that can perform the same types of control instructions. The Sync Engine is most commonly used to trigger DMA transfers without interfering with compute engine instruction scheduling and ordering.

In addition, it is often useful to take engine data-path width and frequency into account when optimizing performance for
a multi-engine operator:


| Device Architecture | Compute Engine | Data-path Width (elements/cycle) | Frequency (GHz) |
| --- | --- | --- | --- |
| Trainium/Inferentia2 | Tensor | 2x128 (input); 1x128 (output) | 2.8 |
| Vector | 128 input/output | 1.12 |
| Scalar | 1.4 |
| GpSimd | 1.4 |


Memory-wise, a NeuronCore-v2 consists of two software-managed on-chip SRAMs, a 24MiB SBUF as the main data storage and a
2MiB PSUM as a dedicated accumulation buffer for Tensor Engine. Both SBUF and PSUM are considered two-dimensional memories
with 128 partitions each, i.e., one SBUF partitions has 192KiB of memory while one PSUM partition has 16KiB. We will cover
more details on data movements with SBUF/PSUM later [here](#arch-sec-data-movement).

The rest of this section will cover the following topics for each compute engine:

* Key functionalities.

* Layout and tile size requirement for input and output tensors.

* Best practices to achieve good performance on the engine.

### Tensor Engine

Tensor Engine (TensorE from now on) is specially designed to accelerate matrix-multiplications (matmuls), as well as other
operators that can be executed using matrix multiplications such as 2D convolutions. We also note that TensorE can be used
for advanced data movement from SBUF to PSUM, including transposition and broadcast
(more discussion below [here](#arch-sec-tensor-engine-alternative-use)).
Architecturally, the engine is built around a [systolic array](https://en.wikipedia.org/wiki/Systolic_array) with
128 rows and 128 columns of processing elements, which streams input data from SBUF and writes output to PSUM.

**Data Types.** TensorE supports [BF16](https://en.wikipedia.org/wiki/Bfloat16_floating-point_format),
FP16, [TF32](https://blogs.nvidia.com/blog/2020/05/14/tensorfloat-32-precision-format/), and cFP8 input matrix data types at a maximum throughput of 92 TFLOPS, as well as 23 TFLOPS for FP32 inputs. TensorE performs
mixed-precision calculations, with accumulations at FP32 precision. Therefore, the output data of a TensorE calculation
is always in FP32.

**Layout.** To understand the layout and tiling constraints of TensorE, let’s visualize its connection to SBUF
and PSUM as below. Note, PSUM partition dimension is purposely rotated 90 degrees compared to SBUF partition dimension
due to systolic array data flow.


> **Figure: tensor engine**
>
> A block diagram showing the Tensor Engine's 128x128 systolic array architecture with its connections to SBUF (input) and PSUM (output), illustrating the data flow for matrix multiplication operations.
>
> This diagram illustrates the core architecture of the NeuronCore Tensor Engine and its relationship with the on-chip memory buffers used for matrix multiplication operations.
>
> **Tensor Engine (Left, Green Square):**
> - Represented as a large green square
> - Dimensions labeled: "128 rows" (vertical, left side) x "128 columns" (horizontal, top)
> - Represents a 128x128 systolic array for matrix multiplication
> - Label: "Tensor Engine" centered in the block
>
> **SBUF - State Buffer (Right, Blue Rectangle):**
> - Represented as a tall blue rectangle to the right of the Tensor Engine
> - Height labeled: "128 partitions" (right side, vertical)
> - Provides input operands to the Tensor Engine
> - Arrow points from SBUF to Tensor Engine, indicating data flow direction
> - Label: "SBUF" centered in the block
>
> **PSUM - Partial Sum Buffer (Bottom, Blue Rectangle):**
> - Represented as a wide blue rectangle below the Tensor Engine
> - Width labeled: "128 partitions" (bottom, horizontal)
> - Receives output/accumulation results from the Tensor Engine
> - Arrow points from Tensor Engine down to PSUM, indicating result flow
> - Label: "PSUM" centered in the block
>
> **Data Flow Pattern:**
> 1. Input data streams from SBUF (right) into the Tensor Engine
> 2. Matrix multiplication is performed in the 128x128 systolic array
> 3. Results accumulate into PSUM (bottom)
>
> **Dimensional Alignment:**
> - SBUF's 128 partitions align with the Tensor Engine's 128 columns (for "moving" matrix)
> - PSUM's 128 partitions align with the Tensor Engine's 128 rows (for output)
> - This creates a natural flow for matrix operations where one operand is stationary and one "moves" through the array
>
> **Key Architecture Insights:**
> - 128x128 = 16,384 multiply-accumulate units operating in parallel
> - SBUF provides streaming input at 10 TB/s bandwidth
> - PSUM accumulates partial results for large matrix products
>
> **Key Elements:**
> - **Tensor Engine (128x128)**: Systolic array for matrix multiplication
> - **SBUF**: Input buffer with 128 partitions feeding the engine
> - **PSUM**: Output accumulator with 128 partitions receiving results
> - **Arrows**: Data flow from SBUF to Engine to PSUM
> - **Partition alignment**: 128 partitions match engine dimensions


Fig. 49 Tensor Engine and SRAM Connectivity.

As shown in the diagram above, TensorE must **read** input matrices from **SBUF** and **write** output matrices to **PSUM**.
PSUM also allows near-memory accumulation of multiple matrix multiplication output tiles (detailed usage discussed
[here](#arch-sec-accumulation-psum)).

In NKI, to perform a multiplication of two matrices, `x[M, K]` and `y[K, N]`, you may invoke the NKI language API
`nki.isa.nc_matmul(x, y)` directly. The returned tile has a shape of `[M, N]` as expected. At the hardware level,
TensorE requires both input tiles to have the **contraction dimension** `K` in the SBUF partition
dimension, that is, the first dimension of input shapes ([Tiling Layout](../programming/tiling-overview.md#nki-tile-layout)).
This ISA requirement is reflected in the low-level API [nki.isa.nc_matmul](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_matmul),
which takes `stationary` and `moving` matrices as input parameters. Therefore, `nki.isa.nc_matmul(x, y)` is a two-step computation:
invoking `nki.isa.nc_transpose(x)` to get `stationary` and then `nki.isa.nc_matmul(stationary, moving)` to get the final result.
In other words, `nki.isa.nc_matmul(stationary[K,M], moving[K,N])` performs a `stationary.T &#64; moving` calculation, which will result
in an output with dimensions `[M,N]`.

For every `nki.isa.nc_matmul(stationary, moving)` call, TensorE executes two distinct Neuron ISA instructions:

* LoadStationary (short for LS): This instruction loads the `stationary` from SBUF and caches it in internal storage of TensorE

* MultiplyMoving (short for MM): This instruction loads the `moving` from SBUF and multiplies `moving` across the pre-loaded
`stationary` matrix from the previous LoadStationary instruction. The output of this instruction is the
output of the `nki.isa.nc_matmul` call written to PSUM.

With the above instruction sequence, we as NKI programmers effectively map input tile `stationary` as the stationary tensor
and input tile `moving` as the moving tensor for TensorE. As a rule-of-thumb for layout analysis, the **free** axis of the
**stationary** tensor always becomes the partition (first) axis of the output tile, while the **free** axis of the
**moving** tensor becomes the free axis of the output. [Fig 50](#fig-arch-matmul) below visualizes this concept
by showing a matrix multiplication in both mathematical and TensorE views.


> **Figure: matmul**
>
> A comprehensive diagram comparing the mathematical view of matrix multiplication with the NeuronCore Tensor Engine implementation view, showing how matrices map to stationary (Tensor Engine), moving (SBUF), and output (PSUM) components.
>
> This diagram is divided into two parts by a vertical dashed line, illustrating how mathematical matrix multiplication maps to NeuronCore hardware.
>
> Part (a) "Mathematical View" (left side) shows standard matrix multiplication:
> - A blue matrix "y" at the top with dimensions N (width) by K (height)
> - A green matrix "x" at the bottom left with dimensions K (width) by M (height)
> - A purple matrix "output" at the bottom right with dimensions N (width) by M (height)
> - The matrices are arranged to show x * y = output multiplication
>
> Part (b) "Tensor Engine View" (right side) shows the hardware mapping:
> - A green matrix labeled "stationary (Tensor Engine)" with dimensions M (stationary_fsize) width by K height - this matrix is loaded into the Tensor Engine and held stationary
> - A blue matrix labeled "moving (SBUF)" with dimensions N (moving_fsize) width by K (rhs_psize) height - this matrix streams from the State Buffer
> - A purple matrix labeled "output (PSUM)" with dimensions N (moving_fsize) width by M (stationary_fsize) height - partial sums accumulate here
> - Arrows show the data flow: stationary and moving matrices feed into the computation, producing output in PSUM
> - A "Copy" arrow shows the PSUM output being copied to a final "output (SBUF)" tensor with dimensions N width by M height, stored in State Buffer
>
> Dimension annotations include:
> - M (stationary_fsize): Free dimension size of stationary matrix
> - N (moving_fsize): Free dimension size of moving matrix  
> - K (lhs_psize, rhs_psize): Contraction dimension
> - PSUM P-dim and SBUF P-dim labels indicate partition dimension orientations
>
> **Key Elements:**
> - **Mathematical View (a)**: Standard matrix multiplication x * y = output
> - **Tensor Engine View (b)**: Hardware-mapped implementation
> - **stationary (Tensor Engine)**: Green matrix held in Tensor Engine
> - **moving (SBUF)**: Blue matrix streamed from State Buffer
> - **output (PSUM)**: Purple partial sum accumulator
> - **output (SBUF)**: Final output copied to State Buffer
> - **M, N, K dimensions**: Matrix dimension labels
> - **Copy arrow**: Data movement from PSUM to SBUF


Fig. 50 MxKxN Matrix Multiplication Visualization.

However, programmers are also free to map `stationary` tile to the moving tensor instead, which would lead to the same output tile
but transposed: `nki.isa.nc_matmul(moving[K,N], stationary[K,M]) = moving.T &#64; stationary = outputT[N, M]`. In fact, mapping high-level input tiles
to the low-level stationary/moving tensors in TensorE is an important layout decision that NKI programmers should consider
to minimize data transposes. Programmers should make this decision based on layout requirements imposed
by the compute engine that is going to consume the matrix multiplication output. See NKI Performance Guide
for more discussion.

**Tile Size.** The `nki.isa.nc_matmul` API enforces the following constraints on the input/output tile sizes:

* `stationary` tensor free axis size (`stationary_fsize`) must never exceed 128, due to the number of PE columns in TensorE.

* `stationary/moving` tensor partition axis size (`stationary_psize/moving_psize`) must never exceed 128, due to the number of PE rows and
also the number of SBUF partitions.

* `moving` tensor free axis size (`moving_fsize`) must never exceed 512, due to the fact that each `nc_matmul` can only write
to a single PSUM bank, which can only hold 512 FP32 elements per PSUM partition.

When the shapes of the input matrices defined in the user-level operator exceed any of the above tile size limitation, we
must tile the input matrices and invoke multiple `nki.isa.nc_matmul` calls to perform the matrix multiplication. Exceeding
the `stationary_fsize` (#1) or `moving_fsize` (#3) tile limitations for M or N should lead to fully independent `nki.isa.nc_matmul`
with disjoint output tiles. However, when `K` exceeds the `stationary_psize/moving_psize` limit, we need to tile the input matrices
in the contraction dimension and invoke multiple `nki.isa.nc_matmul` to accumulate into the *same* output buffer in PSUM.
Refer to the [Tiling Matrix Multiplications](../programming/tutorials/matrix_multiplication.md#tutorial-matmul-tiling)
tutorial for a NKI code example.

#### **Alternative Use Case**

One interesting use case of TensorE is low-latency data reshape within NeuronCore, which typically involves multiplying
a matrix to be reshaped with a compile-time constant matrix filled with zeros and ones.

As an example, we can perform a 128x128 matrix transposition (i.e., swap the free and partition axis of the matrix) using
`nki.isa.nc_matmul(transpose_input, identity)`, where `transpose_input` is the matrix to be transposed and
`identity` is a 128x128 identity matrix. In fact, this is exactly what nki.isa.nc_transpose() does, when TensorE is chosen
as the compute engine.


> **Figure: mm transpose**
>
> A diagram showing how to implement matrix transpose using the Tensor Engine by multiplying with an identity matrix, producing the transposed result in PSUM and then copying to SBUF.
>
> This diagram illustrates a technique for performing matrix transpose using the NeuronCore Tensor Engine's matrix multiplication capability.
>
> The diagram shows four matrices with arrows indicating the computation flow:
>
> **Top left** - Green matrix "x" with dimensions M (width) by N (height), the input matrix to be transposed.
>
> **Top center** - Blue matrix "Identity" with dimensions N (width) by N (height). This shows an identity matrix with 1s on the diagonal and 0s elsewhere. The visible portion shows a 3x3 pattern in the top-left corner (1,0,0 / 0,1,0 / 0,0,1) with dotted lines indicating the full N x N size.
>
> **Bottom left** - Purple matrix "x^T (PSUM)" with dimensions M (width) by N (height), showing the transposed result stored in the Partial Sum buffer. This is the output of multiplying x by the identity matrix.
>
> **Top right** - Blue matrix "x^T (SBUF)" with dimensions N (width) by M (height), the final transposed result in State Buffer.
>
> Arrows show the flow:
> - Arrow from Identity to x (indicating multiplication setup)
> - Arrow from x down to x^T (PSUM)
> - Curved arrow labeled "Copy" from x^T (PSUM) to x^T (SBUF)
>
> The mathematical insight is: x * I = x^T when x is loaded as the moving matrix and I as stationary, effectively transposing the result due to the Tensor Engine's output layout.
>
> **Key Elements:**
> - **x**: Green input matrix [M x N]
> - **Identity**: Blue identity matrix [N x N] with diagonal 1s
> - **x^T (PSUM)**: Purple transposed result in Partial Sum
> - **x^T (SBUF)**: Blue final transposed result in State Buffer
> - **Copy arrow**: Data movement from PSUM to SBUF
> - **M, N dimensions**: Matrix dimension labels
> - **1s and 0s**: Identity matrix pattern


Fig. 51 Transposition.

Similarly, we can broadcast a vector occupying a single partition to M (M <= 128) partitions using `nki.isa.nc_matmul(ones,
broadcast_input, is_stationary_onezero=True)`, where `ones` is a 1xM vector filled with ones and `broadcast_input` is
the vector to be broadcast. In fact, NKI invokes such matmul under the hood when `broadcast_input.broadcast_to((M, broadcast_input.shape[1]))`
is called.


> **Figure: mm broadcast**
>
> A diagram showing how to implement broadcast operations using matrix multiplication, where a vector y is broadcast to a full matrix y_bcast by multiplying with a ones vector, then copying from PSUM to SBUF.
>
> This diagram illustrates a technique for implementing tensor broadcast using the Tensor Engine's matrix multiplication capability on NeuronCore.
>
> At the top of the diagram, two input tensors are shown:
> - A green horizontal tensor of all ones (labeled "1, 1, ..., 1") with dimension M (width) by 1 (height), representing a ones vector
> - A blue horizontal tensor "y" with dimension N (width) by 1 (height), representing the input vector to be broadcast
>
> An arrow points from y to the ones vector, indicating the multiplication operation setup.
>
> Below the inputs, a large purple square tensor "y_bcast (PSUM)" shows the intermediate result stored in the Partial Sum buffer, with dimensions M (width) by N (height). This is the result of the matrix multiplication between the ones vector and y.
>
> A curved arrow labeled "Copy" points from y_bcast (PSUM) to a blue square tensor "y_bcast (SBUF)" on the right, with dimensions N (width) by M (height). This represents copying the broadcast result from PSUM to the State Buffer.
>
> The dimension annotations show:
> - M: Width of the ones vector and the broadcast output
> - N: Height of the input vector y and the broadcast output
> - The tensor is effectively broadcast from shape [1, N] to [M, N]
>
> **Key Elements:**
> - **Ones vector**: Green tensor of all 1s with shape [1, M]
> - **y**: Blue input vector to broadcast with shape [1, N]
> - **y_bcast (PSUM)**: Purple broadcast result in Partial Sum buffer [N, M]
> - **y_bcast (SBUF)**: Blue final result copied to State Buffer [M, N]
> - **Copy arrow**: Data movement from PSUM to SBUF
> - **M, N dimensions**: Size annotations for the broadcast operation


Fig. 52 Partition Broadcast.

In general, we can achieve many more complex data reshapes in TensorE, such as shuffling partitions of a SBUF tensor, by
constructing appropriate zero/one patterns as one of the matmul inputs.

Finally, we can also leverage TensorE for data summation across SBUF partitions (P-dim summation). For example, a vector
laid out across SBUF partitions can be reduced into a single sum using TensorE as shown in the diagram below. Note, this
utilizes only a single PE column of the TensorE; therefore, depending on the surrounding operators, this may not be the
best use of TensorE. If you can do summation within each partition (F-dim summation), see
[nki.isa.tensor_reduce](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_reduce)
for an alternative reduction implementation on Vector Engine. It is recommended to choose the engine based on the natural
layout of your input data to avoid any transpositions.


> **Figure: mm cross partition**
>
> A diagram showing how to perform cross-partition reduction using matrix multiplication, where a vector y is multiplied with a ones vector to produce a scalar sum in PSUM, then copied to SBUF.
>
> This diagram illustrates using matrix multiplication to implement cross-partition reduction (summing across partitions) on NeuronCore.
>
> On the left side, two input vectors are shown vertically:
> - A green vertical tensor of all ones (labeled "1, 1, ..., 1") with dimension 1 (width) by N (height)
> - A blue vertical tensor "y" with dimension 1 (width) by N (height)
>
> An arrow points from y to the ones vector, indicating they will be multiplied together.
>
> Below the inputs, a small purple square labeled "sum (PSUM)" represents the scalar result of the dot product - summing all elements of y. This is stored in the Partial Sum buffer.
>
> A curved arrow labeled "Copy" points from sum (PSUM) to a small blue square "sum (SBUF)" in the upper right, representing the final scalar result copied to the State Buffer.
>
> The key insight is that multiplying a vector by a ones vector computes the sum of all elements:
> - y^T * ones = sum(y)
> - This effectively performs a reduction across the N partition dimension
>
> This technique is useful when you need to sum values across partitions but only have access to the Tensor Engine for computation.
>
> **Key Elements:**
> - **Ones vector**: Green tensor of all 1s with shape [N, 1]
> - **y**: Blue input vector with shape [N, 1]
> - **sum (PSUM)**: Purple scalar sum result in Partial Sum buffer
> - **sum (SBUF)**: Blue final scalar result in State Buffer
> - **Copy arrow**: Data movement from PSUM to SBUF
> - **N dimension**: Length of the vectors being reduced
> - **1 dimension**: Single element width/output


Fig. 53 Cross-Partition Accumulation

As TensorE is the most performant compute engine of the NeuronCore in terms of FLOPS, the goal is to have it execute meaningful
computation at high utilization as much as possible. The above “alternative use cases” stop TensorE from performing *useful*
computations at *high* throughput and therefore, should generally be avoided. However, there are situations where it is
advisable to use them:

* Operators that do not require heavy matmuls anyhow, e.g. normalization, softmax.

* Layout conflicts between producer and consumer engines where broadcast/transpose are absolutely unavoidable (see example
in fused attention tutorial).

#### **Performance Consideration**

As a rule of thumb, TensorE can achieve the best throughput when it runs many back-to-back `nki.isa.nc_matmul` with both
input matrices at the largest possible tiles sizes (`stationary` is 128x128 and `moving` is 128x512). In this ideal
scenario, TensorE sees the below instruction sequence:

* `LoadStationary (LS[0])` (128x128)

* `MultiplyMoving (MM[0])` (128x512)

* `LoadStationary (LS[1])` (128x128)

* `MultiplyMoving (MM[1])` (128x512)

* …

**Cost Model:** TensorE is a deeply pipelined engine; therefore, the engine can have several `LS&MM` instruction pairs
in-flight at a given time. Due to this pipelining nature, it is often *not* useful to use end-to-end execution *latency*
of a single instruction when estimating the instruction cost. Instead, we can focus on the **initiation interval** of
such instructions, that is, the number of cycles between successive instruction launches. Therefore, we can estimate the
cost of an instruction `I` by how soon TensorE can issue the next instruction after `I`.

For the sake of discussion, let’s assume we have many back-to-back `MM` instructions with BF16/FP16/TF32/cFP8 input data
type that reuse a single pre-loaded `stationary` inside TensorE. The initiation interval between subsequent MM instructions in
this case is roughly `max(N, MM_INIT_LATENCY)`, where `MM_INIT_LATENCY` is 64 TensorE cycles on NeuronCore-v2, and `N` is the
free axis size of `moving` of current `MM` (typically set to 512). For FP32 input data type,
the instruction cost is roughly 4x higher than BF16/FP16/TF32/cFP8. Therefore, whenever possible, we recommend down-casting
FP32 input matrix data type to one of BF16/FP16/TF32/cFP8 before performing matrix multiplications.

Figure below visualizes two pipelined `MM` instructions:


> **Figure: mm pipeline**
>
> A timing diagram showing how matrix multiplication operations are pipelined across multiple pipeline stages (0, 1, through P-1), with annotations for initiation interval and full execution latency.
>
> This diagram illustrates the pipelined execution model for matrix multiplication on the NeuronCore Tensor Engine, showing how multiple operations overlap in time.
>
> The diagram shows multiple horizontal timelines representing different pipeline stages:
>
> **Pipeline Stage 0** (top):
> - Shows two consecutive operations MM[0] (blue) and MM[1] (purple)
> - Operations are displayed as rectangular blocks on the timeline
>
> **Pipeline Stage 1** (second row):
> - Same MM[0] and MM[1] operations, but shifted right by one cycle
> - The offset demonstrates the pipeline initiation interval
>
> **Pipeline Stage P-1** (bottom row):
> - Shows the same operations at the end of the pipeline
> - MM[0] and MM[1] blocks appear much later in time
>
> Key timing annotations:
> - **Initiation Interval**: Marked with a green double-headed arrow at the top, showing the time between starting successive operations (approximately one cycle)
> - **1 cycle**: Small annotation showing the cycle boundary
> - **Full execution Latency of MM[0] on TensorE**: A long green double-headed arrow at the bottom spanning from when MM[0] enters Pipeline Stage 0 to when it exits Pipeline Stage P-1
>
> Vertical dashed lines help align the timing across pipeline stages. Ellipsis (...) between Stage 1 and Stage P-1 indicates intermediate pipeline stages not shown.
>
> **Key Elements:**
> - **Pipeline Stage 0, 1, P-1**: Multiple pipeline stages in Tensor Engine
> - **MM[0], MM[1]**: Consecutive matrix multiplication operations
> - **Initiation Interval**: Time between starting new operations
> - **1 cycle**: Basic timing unit
> - **Full execution Latency**: Total time for one operation through all stages
> - **Blue/purple blocks**: Color-coded operations showing overlap
> - **Dashed vertical lines**: Timing alignment markers


Fig. 54 Pipelined multiplyMoving instructions.

**Background LoadStationary:** In typical workloads, TensorE would be alternating between LS and MM instructions with different
input matrices. In order to optimize TensorE’s utilization, we also enable a “background LoadStationary” capability, which
allows loading of the next stationary tensor in parallel to the computation on the current stationary tensor.

As a result, depending on the relative sizes of the `stationary` and `moving` matrices, the overall
TensorE performance can be bounded by either `LS` or `MM` instructions. Figure below visualizes these two cases. In
the ideal scenario where `stationary` and `moving` use the largest tile sizes, TensorE should operate in case (a).


> **Figure: mm bottleneck**
>
> A timing diagram comparing two execution scenarios for matrix multiplication: MultiplyMoving Bounded (where compute is the bottleneck) and LoadStationary Bounded (where memory loading is the bottleneck).
>
> This diagram shows two execution timeline scenarios illustrating different bottleneck conditions in matrix multiplication on NeuronCore, helping developers understand performance limiting factors.
>
> Part (a) "MultiplyMoving Bounded" (top section) shows two parallel timelines:
> - **LoadStationary row**: Shows sequential loading operations LS[0], LS[1], LS[2], LS[3], ... with blocks colored in shades of blue/green. These complete relatively quickly with gaps between them.
> - **MultiplyMoving row**: Shows sequential computation operations MM[0], MM[1], MM[2], MM[3], ... with blocks colored in shades of blue, green, and purple. These operations are longer and continuous, forming the critical path.
>
> In this scenario, LoadStationary completes before MultiplyMoving needs the data, indicating compute is the bottleneck. The computation (MultiplyMoving) takes longer than data loading (LoadStationary).
>
> Part (b) "LoadStationary Bounded" (bottom section) shows two parallel timelines:
> - **LoadStationary row**: Shows the same LS[0] through LS[3] operations, but now they are longer and form a continuous sequence.
> - **MultiplyMoving row**: Shows MM[0] through MM[3] operations with gaps between them, waiting for data to be loaded.
>
> In this scenario, MultiplyMoving must wait for LoadStationary to complete, indicating memory loading is the bottleneck. The computation sits idle while waiting for data.
>
> Both timelines have arrows extending to the right with ellipsis (...) indicating the pattern continues.
>
> **Key Elements:**
> - **LoadStationary (LS)**: Operations loading the stationary matrix into Tensor Engine
> - **MultiplyMoving (MM)**: Matrix multiplication operations with moving matrix
> - **LS[0]-LS[3]**: Individual load operations (blue/teal colors)
> - **MM[0]-MM[3]**: Individual multiply operations (various colors)
> - **MultiplyMoving Bounded (a)**: Compute-limited scenario
> - **LoadStationary Bounded (b)**: Memory-limited scenario
> - **Timeline arrows**: Show execution sequence over time
> - **Gaps vs continuous**: Visual indication of which operation is bottleneck


Possible execution timeline execution with background LoadStationary

**Fast LoadStationary:** Since `LoadStationary` is a pure data movement with no computation, TensorE can perform `LoadStationary`
**up to 4x** faster than a `MultiplyMoving` with the same free axis size. Fast `LoadStationary` has an important performance
implication on `nki.isa.nc_matmul`: When one of the input matrices has a small free axis size and the other has a large
free axis size, we prefer to put the matrix with large free axis as the `stationary` matrix. For example, if we
try to do a vector-matrix multiplication, it is recommended to put the matrix as `stationary` matrix and vector as `moving`
matrix to get the best performance out of TensorE.

### Vector Engine

Vector Engine (VectorE) is specially designed to accelerate vector operations where every element in the output tensor typically
depends on multiple elements from input tensor(s), such as vector reduction and element-wise operators between two tensors.
VectorE consists of 128 parallel vector lanes, each of which can stream data from a SBUF/PSUM partition, perform mathematical
operations, and write data back to each SBUF/PSUM partition in a deeply pipelined fashion.

**Data Types.** VectorE supports all NKI data types (details see [supported data types in NKI](../programming/api/nki.api.shared.md#nki-dtype))
in both input and output tiles. [Arithmetic operations](../programming/api/nki.api.shared.md#nki-aluop)
are performed in FP32, with automatic zero-overhead input and output casting to and from FP32. Refer to `nki.isa` API
reference manual for any instruction-specific data type requirements.

**Layout & Tile Size.** VectorE instructions expect the parallel axis of the input and output data to be mapped to the partition dimension. For
example, the figure below shows reduction add of a NxM matrix along the M dimension. Since each of N rows in the matrix
can be reduced in parallel, the N dimension of the matrix should be mapped to the SBUF partition dimension. Refer to the
nki.isa API manual for
instruction-specific layout constraint of different VectorE instructions.

[![../../../_images/vector_engine_reduce.png](../../../_images/vector_engine_reduce.png)](../../../_images/vector_engine_reduce.png)

Fig. 55 Reduce add on Vector Engine.

In terms of tile size, the majority of VectorE instructions only have limitation on the input/output tile partition dimension
size which must not exceed 128, while the free dimension size can be up to 64K elements for SBUF or 4K elements for PSUM.
However, there are a few notable exceptions, such as nki.isa.bn_stats
which further imposes free dimension size of input tile cannot exceed 512. Refer to the nki.isa API manual <nki.language>
for instruction-specific tile size constraints.

#### Cross-partition Data Movement

The VectorE also supports a limited set of cross-partition data movement within each group of 32 partitions. The figure
below shows connectivity between SBUF and VectorE banks. VectorE consists of four Reshape and Compute banks: each Reshape
Bank connects to 32 SBUF/PSUM partitions and outputs 32 parallel streams of data, while each Compute Bank can process 32
parallel data streams using 32 vector lanes. The Compute Bank can write back to 32 SBUF/PSUM partitions.


> **Figure: vector engine cross partition**
>
> A diagram showing the Vector Engine architecture with 128 SBUF/PSUM partitions mapped to 4 banks (32 partitions each), illustrating how the Reshape Banks and Compute Banks enable cross-partition operations.
>
> This diagram illustrates the internal organization of the Vector Engine, showing how the 128 partitions from SBUF/PSUM are grouped into banks for cross-partition vector operations.
>
> **Left Column - SBUF/PSUM (Input):**
> A vertical stack of 128 partitions labeled "SBUF/PSUM" at the top:
> - **Bank 0 partitions (Purple)**: p[0], p[1], ..., p[31]
> - **Bank 1 partitions (Blue)**: p[32], p[33], ..., p[63]
> - **Bank 2 partitions (Green)**: p[64], p[65], ..., p[95]
> - **Bank 3 partitions (Orange)**: p[96], p[97], ..., p[127]
>
> Dashed arrows on the left indicate input data flow from external sources.
> "128 partitions" label on the left side indicates the total partition count.
>
> **Middle Column - Reshape Banks:**
> Four "Reshape Bank" blocks corresponding to the partition groupings:
> - **Reshape Bank[0]** (Purple): Handles partitions p[0]-p[31]
> - **Reshape Bank[1]** (Blue): Handles partitions p[32]-p[63]
> - **Reshape Bank[2]** (Green): Handles partitions p[64]-p[95]
> - **Reshape Bank[3]** (Orange): Handles partitions p[96]-p[127]
>
> Each partition group connects via solid arrows to its corresponding Reshape Bank.
> Ellipsis (...) between banks indicates the internal processing within each bank.
>
> **Right Column - Compute Banks:**
> Four "Compute Bank" blocks matching the Reshape Banks:
> - **Compute Bank[0]** (Purple)
> - **Compute Bank[1]** (Blue)
> - **Compute Bank[2]** (Green)
> - **Compute Bank[3]** (Orange)
>
> Arrows connect from Reshape Banks to Compute Banks, with ellipsis indicating processing.
> Output arrows on the right indicate results flowing back or to further processing.
>
> **Vector Engine Header:**
> The right two columns (Reshape Banks and Compute Banks) are grouped under the "Vector Engine" label.
>
> **Data Flow:**
> 1. Data enters from SBUF/PSUM partitions
> 2. Partitions are grouped into 4 banks of 32 partitions each
> 3. Reshape Banks reorganize data for cross-partition operations
> 4. Compute Banks perform vector computations
> 5. Results flow out for further processing or storage
>
> **Key Elements:**
> - **128 partitions**: Total SBUF/PSUM partition count
> - **4 banks**: Partitions grouped into banks of 32
> - **Reshape Bank[0-3]**: Data reorganization for cross-partition ops
> - **Compute Bank[0-3]**: Vector computation units
> - **Color coding**: Purple (0-31), Blue (32-63), Green (64-95), Orange (96-127)
> - **Cross-partition capability**: Enables operations across partition boundaries within a bank


Fig. 56 Vector Engine reshape and compute banks.

The Reshape Bank supports the following data movement:

* *32x32 transpose*: Each Reshape Bank can read in 32 elements per SBUF/PSUM partitions and transpose the partition and
free dimension of the incoming 32x32 matrix. This can be invoked by [nki.isa.nc_transpose](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_transpose)
API by selecting VectorE as the execution engine.

* *32 partition shuffle*: Each Reshape Bank can take an arbitrary *shuffle mask*
`SM`* of length 32. The integer value of `SM[i]` indicates the source partition ID (modulo 32) that the Reshape Bank
output stream `i` will get. For example, we can broadcast partition[0] to partition[0-31] using a SM of 32 zeros.
This can be invoked by [nki.isa.nc_stream_shuffle](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_stream_shuffle) API.

Refer [here](#arch-sec-cross-partition-connect)
later in this doc for cross-bank data movement.

#### **Performance Consideration**

**128 Parallel Compute Lanes:** VectorE can perform computation with all 128 vector lanes in parallel, with each lane streaming
data from/to one SBUF/PSUM partition. Therefore, the performance cost of a VectorE instruction using all 128 lanes is the
same as an instruction that uses fewer than 128 lanes.

As a result, we recommend NKI developers to maximize the compute lanes used per VectorE instruction, that is, the partition
axis size of input/output tiles of a single `nki.isa` or `nki.language` compute API call. When the partition axis size
of input tiles is inevitably fewer than 128 partitions due to high-level operator definition, we could adopt an optimization
called “partition vectorization” by packing multiple “small” VectorE instructions of the same operation into a single “large”
Vector instruction. Refer to NKI Performance Guide for more detailed discussion of this optimization.

**Cost Model:** In the most common cases where the free axis size (`N`) of the input tile(s) is sufficiently large
(`N > 128`), the execution cost of an instruction on VectorE is correlated to `N`:

* If there is only one input tile, most VectorE instructions can execute in roughly `N` cycles (example:
[nki.isa.tensor_scalar](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_scalar))

* If there are two input tiles, the instruction can execute in roughly `2N` cycles (example: nki.isa.tensor_tensor)

There are a few exceptions to the above rule, depending on the data types and instruction type. See
[NKI ISA API doc](../programming/api/nki.isa.md)
for instruction-specific instruction cost details.

In the rare cases where VectorE is running many back-to-back instructions either with `N << 128` or with every instruction
depending on the output tile of the previous instruction, we need to add a static instruction overhead of 100 engine cycles
to the above execution cost estimate.

The above rules are for general guidance only. To find out the exact instruction costs for your NKI kernel, you may capture
a detailed instruction execution trace on device using [neuron-profiler](../optimization/use-neuron-profile.md).

### Scalar Engine

Scalar Engine (ScalarE) is specially designed to accelerate scalar operations where every element in the output tensor only
depends on one element of the input tensor. In addition, ScalarE provides hardware acceleration to evaluate non-linear functions
such as Gelu and Sqrt. The currently supported set of non-linear functions is listed in [here](../programming/api/nki.api.shared.md#nki-act-func).
It it worth noting that we can support any new non-linear functions on ScalarE as they come up in new ML model architectures
through Neuron SDK software updates. Similar to VectorE, ScalarE consists of 128 parallel lanes, each of which can stream
data from a SBUF/PSUM partition, perform mathematical operations, and write data back to each SBUF/PSUM partition in a deeply
pipelined fashion.

**Data Types.** ScalarE supports all NKI data types (details see [supported data types in NKI](../programming/api/nki.api.shared.md#nki-dtype))
in both input and output tiles. All internal computation is performed in FP32,
with automatic zero-overhead input and output casting to and from FP32.

**Layout & Tile Size.** ScalarE typically evaluates scalar operations (such as, nki.language.gelu), which does not impose
any input/output tile layout constraints. However, there are additional hardware features in ScalarE that will have layout
constraints similar to VectorE (more discussion later).

In terms of tile size, ScalarE instructions only have limitation on the input/output tile partition dimension size which
must not exceed 128, while the free dimension size can be up to 64K elements for SBUF or 4K elements for PSUM.

#### Pipelined Multiply-Add

Each ScalarE compute lane also supports an additional multiply-add **before** the non-linear function (`func`) is applied
in a pipeline fashion. Mathematically, ScalarE implements:


```python
# Case 1: scale is SBUF/PSUM vector
# Input: 2D in_tile, 1D scale, 1D bias
# Output: 2D out_tile
for lane_id in range(in_tile.shape[0]):
   for k in range(in_tile.shape[1])
    out_tile[lane_id][k] = func(in_tile[lane_id][k] * scale[lane_id]
                                    + bias[lane_id])

# Case 2: scale is a compile-time scalar constant in the instruction
for lane_id in range(in_tile.shape[0]):
   for k in range(in_tile.shape[1])
    out_tile[lane_id][k] = func(in_tile[lane_id][k] * scale
                                    + bias[lane_id])
```


This functionality can be invoked using the [nki.isa.activation](../programming/api/api-nki-isa-scalar.md#nki-isa-activation)
API by specifying a `scale` for multiplication and `bias` for addition. The scale can either be a tile from SBUF/PSUM
with one element/partition or a compile-time constant. On the other hand, the bias can only be a tile from SBUF/PSUM with
one element/partition. A useful mental model for this capability is combining a [nki.isa.tensor_scalar](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_scalar)
instruction with a non-linear function evaluation into a single instruction (2x speed-up than two separate instructions).

#### Pipelined Reduction

Each ScalarE compute lane also supports reduction **after** the non-linear function (`func`) is applied
in a pipeline fashion. On NeuronCore-v2, the reduction operator can only be addition.

Mathematically, ScalarE with accumulation enabled implements:


```python
# Input: 2D in_tile, 1D scale (similarly for scalar scale), 1D bias
# Output: 2D out_tile, 1D reduce_res
for lane_id in range(in_tile.shape[0]):
  for k in range(in_tile.shape[1]):
    out_tile[lane_id][k] = func(in_tile[lane_id][k] * scale[lane_id]
                                 + bias[lane_id])
    reduce_res[lane_id] += out_tile[lane_id][k]
```


This functionality can be invoked using the [nki.isa.activation_reduce](../programming/api/api-nki-isa-scalar.md#nki-isa-activation_reduce)
API by specifying `reduce_op` as `nki.language.add` and `reduce_res` as
the output reduction tile, passed by reference.

A useful mental model for this capability is combining a [nki.isa.activation](../programming/api/api-nki-isa-scalar.md#nki-isa-activation)
instruction with a [nki.isa.tensor_reduce](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_reduce) into a single API,
which returns results from **both** APIs. Note,
[nki.isa.activation_reduce](../programming/api/api-nki-isa-scalar.md#nki-isa-activation_reduce)
invokes two back-to-back ISA instructions on hardware, Activate and ActReadAccumulator. The Activate instruction
performs the regular computation as specified in [nki.isa.activation](../programming/api/api-nki-isa-scalar.md#nki-isa-activation) and also
reduction at no additional cost. The reduction result is cached inside ScalarE after Activate.
The ActReadAccumulator instruction is a low cost (roughly 64 ScalarE cycles on NeuronCore-v2)
instruction to write the internal reduction result back to SBUF/PSUM, one element per partition.

#### Performance Consideration

All the performance notes discussed for [Vector Engine](#arch-sec-vector-engine-perf)
earlier are applicable to Scalar Engine, with one exception regarding instruction cost for two input tensors - ScalarE can
only read up to one input tensor per instruction.

**Instruction Combination.** All `nki.isa.activation` instructions have the same execution cost, regardless of whether
we enable the scale multiplication or bias add. Therefore, it is recommended to combine such multiply-add operations with
non-linear function evaluation into a single ScalarE instruction if the computation allows it. This is highly useful for
ML operators that are **not** TensorE heavy (not matmul-bound). Softmax is one such example, where we typically subtract
the maximum value of the input elements before evaluating exponential function for numerical stability.

### GpSimd Engine

GpSimd Engine (GpSimdE) is intended to be a general-purpose engine that can run any ML operators that cannot be lowered
onto the other highly specialized compute engines discussed above efficiently, such as applying a triangular mask to a tensor.

A GpSimdE consists of eight fully programmable processors that can execute arbitrary C/C++ programs. Therefore, this engine
provides the hardware support for [Neuron Custom Operator.](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/neuron-customops/programming-guide/custom-c%2B%2B-operators-devguide.html)
In addition, each processor is a 512-bit vector machine that can run high-performance vectorized kernels. Every `nki.isa`
API running on GpSimdE such as [nki.isa.iota](../programming/api/api-nki-isa-utility.md#nki-isa-iota)
uses a vectorized kernel implementation that Neuron engineers hand-tune for the underlying processor ISA.

**Data Types.** Each processor in GpSimd supports vectorized computation for

* 16x FP32/INT32/UINT32, or

* 32x FP16/INT16/UINT16, or

* 64x INT8/UINT8

This is in contrast to ScalarE/VectorE which can only perform arithmetic operations in FP32. However, if the GpSimdE program
chooses to, it can also access SBUF data of any [supported data types in NKI](../programming/api/nki.api.shared.md#nki-dtype)
and perform data casting to- and from-FP32 at no throughput cost similar to VectorE/ScalarE.

**Layout & Tile Size.** The layout and tile size requirements of GpSimdE highly depend on semantics of the exact instruction.
Refer to the [nki.isa API reference guide](../programming/api/nki.isa.md)
for these requirements.

**Memory Hierarchy.** In Trainium/Inferentia2, each GpSimdE processor has 64KB of local data RAM, also called tightly-coupled
memory (TCM) as discussed in [Neuron Custom Operator](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/neuron-customops/programming-guide/custom-c%2B%2B-operators-devguide.html).
The TCM is configured with a 3-cycle access latency and 512-bit data width. Therefore, TCM is often used to store intermediate
computation results within a Neuron Custom Operator or GpSimdE instruction.

The eight processors in GpSimdE also have a high-bandwidth read/write interface connected to the SBUF.
[Figure 57](#fig-gpsimd-sbuf-connectivity) below illustrates the GpSimdE connectivity to SBUF. Each processor connects
to 16 SBUF partitions for both reading and writing: processor[0] connected to partition[0:15], processor[1] to partition[16:31]
and so on. Each processor can programmatically send tensor read/write requests to SBUF to access data from the connected
partitions. On the read side, once a read request is processed, the tensor read interface can deliver up to 512-bit of data
from all 16 connected partitions collectively (up to 32-bit per partition) to the processor per cycle, which matches the
512-bit SIMD width. Similarly, on the write side, the tensor write interface can accept 512-bit of data for writing back
to the connected SBUF partitions per cycle.


> **Figure: gpsimd sbuf connectivity**
>
> A connectivity diagram showing how SBUF (State Buffer) partitions map to GpSimd Engine cores, with 8 partition groups each connecting to one of 8 GpSimd cores via bidirectional arrows.
>
> This diagram illustrates the memory-to-compute connectivity between the State Buffer (SBUF) and the GpSimd (General Purpose SIMD) Engine in NeuronCore architecture.
>
> On the left side, a column labeled "SBUF" (in italics) contains 8 stacked rectangular blocks in light blue/cyan color, each representing a group of 16 partitions:
> - Partition [0-15] at the top
> - Partition [16-31]
> - Partition [32-47]
> - Partition [48-63]
> - Partition [64-79]
> - Partition [80-95]
> - Partition [96-111]
> - Partition [112-127] at the bottom
>
> On the right side, a column labeled "GpSimd Engine" (in italics) contains 8 stacked rectangular blocks in light purple/lavender color, each representing a compute core:
> - Core[0] at the top
> - Core[1]
> - Core[2]
> - Core[3]
> - Core[4]
> - Core[5]
> - Core[6]
> - Core[7] at the bottom
>
> Between each SBUF partition group and its corresponding GpSimd core, bidirectional arrows (pointing both left and right) indicate two-way data flow. Each partition group connects exclusively to one core: Partition [0-15] connects to Core[0], Partition [16-31] connects to Core[1], and so on through Partition [112-127] connecting to Core[7].
>
> **Key Elements:**
> - **SBUF**: State Buffer memory divided into 8 partition groups
> - **Partition [0-15] through [112-127]**: Eight groups of 16 partitions each
> - **GpSimd Engine**: General Purpose SIMD compute engine with 8 cores
> - **Core[0] through Core[7]**: Eight parallel compute cores
> - **Bidirectional arrows**: Two-way data flow between partition groups and cores
> - **One-to-one mapping**: Each partition group maps to exactly one GpSimd core


Fig. 57 Connectivity between GpSimdE and SBUF.

#### **Performance Consideration**

**128 Parallel Compute Lanes:** Similar to VectorE and ScalarE, GpSimdE has 128 parallel compute lanes for 32-bit computation
data types across SIMD lanes of all eight processors. Therefore, it is desirable to invoke GpSimdE instructions that will
utilize all the parallel compute lanes, typically through accessing all 128 SBUF partitions for input and output. In addition,
since each processor can also handle 32-wide 16-bit or 64-wide 8-bit data type computation, GpSimdE can effectively support
256 or 512 parallel compute lanes internally.

**Cost Model:** Unlike VectorE/ScalarE, there is no rule-of-thumb to estimate execution cost of a GpSimdE instruction. Refer
to the [nki.isa](../programming/api/nki.isa.md)
API reference manual to find out instruction-specific latency estimates.

## Data Movement

In this section, we will dive into the memory subsystem and discuss how to perform data movement between different memories
and also how to do it efficiently. As a reminder, there are three main types of memory on a NeuronDevice: HBM, SBUF, and
PSUM, from highest to lowest capacity. Figure below shows the specifications of these memories and their connectivity
for one NeuronCore-v2:


> **Figure: memory hierarchy**
>
> A hierarchical architecture diagram showing the NeuronCore memory system with on-chip components (PSUM, compute engines, SBUF) and off-chip HBM, connected through DMA engines.
>
> This diagram illustrates the memory hierarchy of the NeuronCore architecture, organized vertically with on-chip components at the top and off-chip memory at the bottom.
>
> At the top of the on-chip section (enclosed in a dashed rectangle), the "PSUM" block (peach/orange color) spans the full width, representing the Partial Sum accumulator memory.
>
> Below PSUM, four compute engine blocks are arranged horizontally:
> - "TensorE" (Tensor Engine) - leftmost
> - "VectorE" (Vector Engine) - second from left
> - "ScalarE" (Scalar Engine) - third from left
> - "GpSimdE" (General Purpose SIMD Engine) - rightmost
>
> Each compute engine has bidirectional arrows connecting to both PSUM above and SBUF below.
>
> In the middle, the "SBUF" block (green color) spans the full width, representing the State Buffer - the main on-chip SRAM.
>
> Below SBUF, multiple "DMA" blocks are shown (four visible plus ellipsis indicating more), each with bidirectional arrows connecting to SBUF above and HBM below.
>
> At the bottom (in the off-chip section, below the dashed line), the "HBM" block (light blue) represents the High Bandwidth Memory - the external device memory.
>
> A bracket on the right side labels the upper section as "on-chip" and the lower section (HBM) as "off-chip", with a dashed line separating them.
>
> The bidirectional arrows throughout indicate data can flow in both directions between all connected components.
>
> **Key Elements:**
> - **PSUM**: Partial Sum accumulator at top (peach/orange)
> - **TensorE**: Tensor Engine compute unit
> - **VectorE**: Vector Engine compute unit
> - **ScalarE**: Scalar Engine compute unit
> - **GpSimdE**: General Purpose SIMD Engine
> - **SBUF**: State Buffer - main on-chip SRAM (green)
> - **DMA**: Multiple DMA engines for memory transfers
> - **HBM**: High Bandwidth Memory - off-chip device memory (light blue)
> - **on-chip / off-chip**: Labels indicating memory hierarchy levels
> - **Bidirectional arrows**: Data flow between all components


Fig. 58 Memory hierarchy.

As shown in the above figure, data movement between HBM and SBUF is performed using on-chip DMA
(Direct Memory Access) engines, which can run in
parallel to computation within the NeuronCore. Data movement between PSUM and SBUF is done through ISA instructions on the
compute engines. However, different compute engines have different connectivity to SBUF/PSUM as indicated by the arrows
in the figure. In addition, NeuronCore-v2 has the following restrictions:

* VectorE and GpSimdE cannot access SBUF in parallel.

* VectorE and ScalarE cannot access PSUM in parallel.

Therefore, VectorE and GpSimdE instructions that access SBUF must be serialized, similarly for VectorE and ScalarE instructions
that access PSUM. This is enforced by Neuron Compiler during NKI kernel compilation, so NKI developers are not required
to program such serializations.

The rest of this section will discuss the following topics in detail:

* Data movement between HBM and SBUF using DMAs.

* Accessing SBUF/PSUM tensors using compute engines.

* In-memory accumulation using TensorE and PSUM.

### Data movement between HBM and SBUF using DMAs

Each NeuronCore-v2 is equipped by 16 parallel DMA engines that can perform data movement between any addressable
memories in the system. Here, we focus on using these DMA engines to move data between the local SBUF and HBM.
Each DMA engine can process one **DMA transfer** at a time driving a peak bandwidth of 27 GiB/s, but all DMA engines
can process different DMA transfers in parallel.

Each DMA transfer can gather a list of source **DMA buffers** and then scatter the data into another list of destination
DMA buffers. Data within a DMA buffer must be continuous in the memory address map. There is some performance overhead
at both DMA buffer and transfer levels, both of which can be amortized by moving a sufficiently
large amount of data (more discussion below).

Next, let’s examine how HBM and SBUF are laid out in the device memory address map. On one hand,
HBM is logically a one-dimensional memory and hence occupies a flat chunk of continuous addresses in the
address map. In the most common cases, an HBM tensor in NKI is also contiguous in the HBM address space.

On the other hand, SBUF is considered a two-dimensional memory with 128 partitions as discussed earlier [here](#arch-sec-neuron-core-engines).
[Figure 59](#fig-arch-sbuf-addr-space)
shows how SBUF addresses fit in the device
address map. `sbuf_base_addr` is a 64-bit address dependent
on which NeuronCore-v2 on the device the SBUF is located in. The SBUF addresses start from the first byte of partition 0,
increment along the free dimension first and then advance onto the next partition.


> **Figure: sbuf addr space**
>
> A diagram illustrating the State Buffer (SBUF) address space organization showing the two-dimensional addressing scheme with Partition dimension (128 partitions) and Free dimension, along with base address offsets.
>
> This diagram shows the memory organization of the SBUF (State Buffer) in NeuronCore, illustrating how the address space is structured along two dimensions critical for NKI programming.
>
> **Overall Layout:**
> The SBUF is represented as a rectangular grid with the Partition dimension on the vertical axis and the Free dimension on the horizontal axis.
>
> **Address Markers (Top, Green Text):**
> Three address markers are shown along the top of the diagram indicating memory positions in the Free dimension:
> - **sbuf_base_addr**: Starting address (leftmost position)
> - **sbuf_base_addr+1**: Second position
> - **sbuf_base_addr+192KiB**: Position at 192 KiB offset (right side)
>
> **Additional Address (Left Side, Green Text):**
> - **sbuf_base_addr+256KiB**: Shows the address after traversing through partition addresses
>
> **Partition Dimension (Right Side Labels):**
> The vertical axis shows partition indices with labels:
> - Partition 0 (top)
> - Partition 1
> - Partition 2
> - Partition 3
> - ... (ellipsis with three dots indicating partitions 4-125)
> - Partition 126
> - Partition 127 (bottom)
>
> **Dimension Labels:**
> - **Partition (P) Dimension**: Labeled on the right side with bidirectional arrow
> - **Free (F) Dimension**: Labeled at the bottom with bidirectional arrow
>
> **Visual Elements:**
> - Green highlighted cells at the top-left corner showing the first few elements
> - Dashed lines indicating address boundaries
> - Arrow from sbuf_base_addr pointing to the top-left cell
> - The grid structure shows 128 rows (partitions) and variable columns (free dimension)
>
> **Memory Layout Interpretation:**
> - The Free dimension extends horizontally with 192 KiB of addressable space per partition
> - The Partition dimension has 128 partitions (0-127)
> - Total SBUF size: 128 partitions x 192 KiB = 24 MiB (approximate)
> - The 256 KiB offset represents wrapping through partition addresses
>
> **Key Elements:**
> - **sbuf_base_addr**: Starting address of SBUF allocation
> - **128 Partitions**: Partition indices 0-127 along vertical axis
> - **192 KiB per partition**: Free dimension size per partition
> - **Two-dimensional addressing**: P (partition) and F (free) coordinates
> - **Green cells**: Highlighted memory elements being accessed
> - **Partition stride**: Moving down increments partition, not contiguous in memory


Fig. 59 SBUF memory address space.

As discussed in [NKI Language Guide](../programming/nki-language-guide.md),
an SBUF tensor in NKI spans one or more partitions, with data starting at the same offset:

[![../../../_images/pm-layout.png](../../../_images/pm-layout.png)](../../../_images/pm-layout.png)

Fig. 60 SBUF tensor.

As a result, a data movement involving `tensor` in SBUF will require at least `tensor.shape[0]`, i.e., P dim size,
different DMA buffers, since slices of tensor data from different SBUF partitions occupy non-contiguous memory
in the address space. If the tensor data slice within each SBUF partition is not contiguous in the F dimension,
more DMA buffers will need to be unrolled along the F dim. These DMA buffers are typically grouped into different
DMA transfers so that multiple DMA engines can participate in the data movement to maximize memory bandwidth utilization.

In NKI, moving data from HBM to SBUF and from SBUF to HBM are done with calls to the [nki.isa.dma_copy](../programming/api/api-nki-isa-memory.md#nki-isa-dma_copy) API. Neuron Compiler is responsible for converting each NKI API call to DMA transfers and
assigning these transfers to different DMA engines. As an example, loading a 128x512 FP32 HBM tensor to SBUF is best
done through 16 DMA transfers (one per DMA engine), each moving a scatter-gather list of 8 DMA buffers:


```python
import nki.language as nl
import nki.isa as nisa
tile = nl.ndarray((128, 512), dtype=in_tensor.dtype, buffer=nl.sbuf)
nisa.dma_copy(dst=tile, src=in_tensor[0:128, 0:512])
```


To achieve good performance out of the DMAs, we generally aim to:

* Move a large amount of contiguous data in each DMA buffer to amortize DMA buffer overhead

* Move a large amount of data in each DMA transfer to amortize DMA transfer overhead.

* Invoke as many parallel DMA transfers on the available DMA engines as possible.

These goals ultimately boil down to a quick optimization rule: maximize **both free (4KiB or above) and partition
(ideally 128) dimension sizes** when moving tensors between SBUF and HBM using `nki.language.load`
and `nki.language.store`. Refer to the
[NKI Performance Guide](../optimization/nki_perf_guide.md) for more information
on optimizing performance of data movements between HBM and SBUF.

### Accessing SBUF/PSUM tensors using compute engines

[Figure 61](#fig-arch-data-streaming) shows a simplified timeline of how compute engines
**stream** data in and out of on-chip SRAM (SBUF or PSUM).
Refer to [Figure 48](#fig-arch-neuron-core-v2) for the available connectivity between engines and SBUF/PSUM.
At a high level, the compute engines are able to pipeline
data reads, computation and writes along the F dimension of the src/dst tensors.
In every cycle, each engine can read 128 elements across 128 SBUF/PSUM partitions,
perform a computation on previously
read 128 elements, and write 128 previously computed results to SBUF/PSUM.
In other words, the P axis of a tensor
is the *parallel* dimension for SBUF/PSUM data accessing, while the F axis of the tensor is the *time* dimension for data
accessing.


> **Figure: data streaming**
>
> A time-sequenced diagram showing how data streams through a compute engine pipeline, illustrating the progression from source tensor (SBUF/PSUM) through the compute engine to destination tensor across multiple time steps (Time = 0, 1, through N).
>
> This diagram illustrates the data streaming execution model in NeuronCore architecture across three time snapshots, showing how tensor data flows through the compute pipeline.
>
> At Time = 0 (top section), the source tensor (SBUF/PSUM: src_tensor) is shown on the left as a light blue rectangular block with the partition (P) dimension running vertically and the free (F) dimension horizontally. Multiple arrows point from the source tensor through vertical bars (representing data lanes) toward the Compute Engine in the center, shown as a peach/orange colored block. The destination tensor (SBUF/PSUM: dst_tensor) on the right is shown with dashed outlines, indicating it has not yet received data.
>
> At Time = 1 (middle section), the pattern continues with arrows flowing from the source tensor through the compute engine. Now arrows also emerge from the right side of the compute engine toward the destination tensor, which remains outlined with dashed lines, showing data beginning to flow through the pipeline.
>
> At Time = N (bottom section), the full pipeline is active. The source tensor shows ellipsis (...) indicating ongoing data reads, the Compute Engine shows internal "Engine pipelines" with multiple processing stages indicated by ellipsis, and arrows flow to the destination tensor which also shows ellipsis indicating ongoing data writes.
>
> **Key Elements:**
> - **SBUF/PSUM: src_tensor**: Source tensor providing input data (light blue)
> - **Compute Engine**: Central processing unit (peach/orange color) with internal pipelines
> - **SBUF/PSUM: dst_tensor**: Destination tensor receiving output data (dashed outlines initially)
> - **Partition (P) Dimension**: Vertical axis of tensors
> - **Free (F) Dimension**: Horizontal axis of tensors
> - **Time = 0, 1, N**: Three time steps showing pipeline progression
> - **Arrows**: Data flow direction from source through compute to destination
> - **Engine pipelines**: Internal pipeline stages within the compute engine


Fig. 61 Data streaming between SBUF and compute engine.

When accessing SBUF/PSUM tensors in an instruction, we need to follow different rules in the P and F dimensions. First,
hardware does not allow P dimension striding when accessing data from a single SBUF/PSUM tensor. Therefore, a valid src/dst
tensor of an instruction must occupy a continuous number of partitions. In addition, the hardware further enforces which
partition a tensor can start from (`start_partition`) based on the number of partitions the tensor occupies (`num_partition`). This is currently handled by the tensor allocator in Neuron Compiler during NKI kernel compilation process:

* If `64 < num_partition <= 128`, `start_partition` must be 0

* If `32 < num_partition <= 64`, `start_partition` must be 0 or 64

* If `0 < num_partition <= 32`, `start_partition` must be one of 0/32/64/96

On the other hand, data accessing along the free dimension is a lot more flexible: the src/dst tensor of an engine
instruction can support up to four-dimensional tensorized access pattern with a stride in each dimension
within each partition. At the ISA level,
each F axis in the tensor can have a size expressed in `uint16` and a stride expressed in `int16`, measured in data elements.
As an example, if the tensor data type is BF16, and the stride of the most-minor F dimension is set to 10, then we will
stride across 20B within a partition at a time. Refer to Tile Indexing in NKI Programming Guide
to learn about how to index SBUF/PSUM tensors to achieve F dimension striding in NKI syntax.

Lastly, as implied in [Figure 61](#fig-arch-data-streaming),
when accessing a SBUF/PSUM tensor, all active partitions must follow the same F dimension access pattern. In other words,
at every time step, the engine read/write interface will access data elements at the same *offset* within each active partition.

#### Cross-Partition Connectivity

The majority of VectorE/ScalarE/GpSimdE instructions on NeuronCore-v2 require `src_tensor` and `dst_tensor` to occupy
the same number of partitions. When the number of partitions involved exceeds 64, by the `start_partition` rule discussed
above, the src_tensor and dst_tensor in such cases must both start from partition 0. Therefore, we effectively cannot perform
any cross-partition data movement when `num_partition > 64` : each partition of `src_tensor` data will eventually flow
into the corresponding partition in `dst_tensor`.

However, when `num_partition < 64`, VectorE/ScalarE/GpSimdE on NeuronCore-v2 supports two styles of cross-partition
SBUF/PSUM data movement patterns: 1) cross-half movement for `32 < num_partition <= 64` and 2) cross-quadrant movement
for `0 < num_partition <= 32`. Figure below illustrates these two patterns for `num_partition=64` and `num_partition=32`.
The shaded portion of the `Engine` block indicates the active lanes for the given instruction. With these movement patterns,
each partition in `src_tensor` still has a one-to-one mapping to each partition in `dst_tensor`.


> **Figure: cross quadrant**
>
> A technical diagram illustrating two types of tensor data movement patterns: Cross-Half Movement and Cross-Quadrant Movement, showing how data flows between SBUF/PSUM source tensors and destination tensors through the VectorE/ScalarE/GpSimdE compute engines.
>
> This diagram is divided into two parts, (a) and (b), showing different partition-based data movement patterns in NeuronCore architecture.
>
> In part (a) "Cross-Half Movement", the left side shows a source tensor (SBUF/PSUM: src_tensor) with partitions labeled from Partition 0 at the top through Partition 127 at the bottom. The partition dimension (P) runs vertically while the free dimension (F) runs horizontally. The source tensor shows two distinct groups: partitions 0-1 (light blue) in the upper portion and partitions 63-66 (orange/tan colors) in the middle, with Partition 127 at the bottom. A gray rectangular block in the center represents the VectorE/ScalarE/GpSimdE compute unit. The right side shows the destination tensor (SBUF/PSUM: dst_tensor) with the same partition structure, where data from the lower half of source partitions appears in the upper half of destination partitions and vice versa.
>
> In part (b) "Cross-Quadrant Movement", the layout is similar but with four distinct color-coded groups of partitions: partitions 0-1 (light blue), partitions 31-33 (light green), partitions 63-65 (orange), partitions 95-97 (light purple), and partition 127. This shows a more complex four-way redistribution pattern where data is exchanged across four quadrants of the partition space.
>
> **Key Elements:**
> - **SBUF/PSUM: src_tensor**: Source tensor on the left side with partition dimension (P) vertical and free dimension (F) horizontal
> - **VectorE/ScalarE/GpSimdE**: Central gray compute engine block processing the data movement
> - **SBUF/PSUM: dst_tensor**: Destination tensor on the right side receiving redistributed data
> - **Partition 0-127**: Full range of 128 partitions in the tensor
> - **Cross-Half Movement (a)**: Two-way data exchange between upper and lower halves
> - **Cross-Quadrant Movement (b)**: Four-way data exchange across quadrants with color-coded partitions (blue, green, orange, purple)
> - **Ellipsis (...)**: Indicates additional partitions not explicitly shown in the diagram


Fig. 62 Cross-partition connectivity.

#### Performance Consideration

**Access pattern.** As discussed previously in the context of compute engine utilization, it is recommended to use as many
partitions as possible when accessing SBUF/PSUM tensors to saturate the available data streaming bandwidth. In addition,
accessing with a large stride in the most-minor (fastest) F dimension will incur performance penalty. When the most-minor
F dimension stride is less than 16 bytes, SBUF/PSUM on NeuronCore-v2 can supply a peak bandwidth of 128 elements/cycle at
1.4 GHz for each tensor read/write interface. A 16-byte stride is equivalent to 4 elements for 32-bit data types, 8 elements
for 16-bit data types or 16 elements for 8-bit data types.
If the most-minor F dimension stride exceeds 16 bytes, the achievable bandwidth of each tensor read/write interface will
be half of the peak bandwidth, which translates to roughly 50% performance hit on the instructions.

**Concurrent SBUF/PSUM accesses by engines.** As mentioned earlier, NeuronCore-v2 has the following on-chip RAM access restrictions:

* Vector Engine and GpSimd Engine cannot access SBUF in parallel

* Vector Engine and Scalar Engine cannot access PSUM in parallel

Despite these restrictions, SBUF is capable of driving peak bandwidth in each tensor read/write interface connected to VectorE/ScalarE/TensorE
or GpSimdE/ScalarE/TensorE *simultaneously* without bandwidth interference. Similarly, PSUM can drive peak bandwidth for
VectorE/TensorE or ScalarE/TensorE *simultaneously*.

**Tensor access overhead.** Initiating a tensor access request from an engine to its SBUF/PSUM read/write interface incurs
a static overhead approximately 60 cycles on NeuronCore-v2. Compute engines can typically hide some of this latency through
instruction level parallelism. However, it is still highly recommended to access tensors with large P and F dimension sizes
whenever possible to amortize this overhead.

### Near-memory accumulation in PSUM

As shown in [Figure 48](#fig-arch-neuron-core-v2),
both VectorE and ScalarE have read and write access to PSUM, while TensorE only has write access. In fact, PSUM is designed
to be a landing buffer for TensorE with near-memory accumulation capabilities that allows read-accumulate-write to every
4B element in memory. Note, this accumulation mechanism can *only* be controlled by TensorE. VectorE and ScalarE can only
access PSUM like a regular SRAM similar to SBUF.

Next, let’s discuss how TensorE can write outputs to PSUM. As previously discussed, PSUM is organized into 128 *partitions,*
each consisting of 16KB of memory. Each partition is further divided into 8 PSUM banks, with each bank holding up to 512
32-bit values. The output tile of a TensorE matrix multiplication instruction (`nki.isa.nc_matmul`) must **fit** into
one PSUM bank per partition, which is the fundamental reason for
the [free dimension size limitation](#arch-matmul-tile-size) for the `moving` tensor.
Every `nc_matmul` instruction can choose whether to *override* existing bank data with instruction output or *accumulate*
instruction output into existing bank data element-wise.

The accumulation mode of PSUM is particularly useful when the high-level matmul operator has a contraction dimension (i.e.,
`stationary/moving` partition dimension of `nki.isa.nc_matmul`) greater than 128. As an example, let’s assume the following
matmul dimensions:

* `x.shape = [128, 256]`

* `y.shape = [256, 512]`

Figure below shows this matmul mathematically and also how we would tile the contraction dimension. With tiling, we slice
both `x` and `y` in the contraction dimension to get `[x0, x1]` and `[y0, y1]` input tiles. To get the
final output result, we need to perform:

* output0 = matmul(x0, y0)

* output1 = matmul(x1, y1)

* output = output0 + output1


> **Figure: mm tiling**
>
> A two-part diagram showing matrix multiplication tiling strategy: (a) Mathematical View showing how matrices are divided into tiles, and (b) 3 Steps with Tiling showing the sequential computation and accumulation process.
>
> This diagram explains the tiling approach for matrix multiplication when matrices exceed the Tensor Engine's tile size limits.
>
> **Part (a) Mathematical View** (top section):
> Shows the full matrix multiplication setup:
> - A blue matrix "y" at top, divided into y_0 and y_1 by a red dashed line, with dimensions 512 (width) by 256 (height)
> - A green matrix "x" divided into x_0 and x_1, with dimensions 256 (width) by 128 (height)
> - A purple "output" matrix with dimensions 512 (width) by 128 (height)
> - Dimension annotations: 512, 256, 128 showing the matrix sizes
>
> **Part (b) 3 Steps with Tiling** (bottom section):
> Shows the sequential computation process:
>
> **Step 1**:
> - Blue tile y_0 (512 x 128) with red dashed border indicating current tile
> - Green tile x_0 (128 x 128)
> - Purple output_0 (512 x 128) - first partial result
>
> **Step 2**:
> - Blue tile y_1 (512 x 128) with red dashed border
> - Green tile x_1 (128 x 128)
> - Purple output_1 (512 x 128) - second partial result
>
> **Step 3**:
> - Shows output_0 + output_1 = output
> - Purple tiles being summed to produce final result
>
> The red dashed borders indicate which tiles are currently being processed in each step.
>
> **Key Elements:**
> - **y_0, y_1**: Tiles of the y matrix (blue)
> - **x_0, x_1**: Tiles of the x matrix (green)
> - **output_0, output_1**: Partial output tiles (purple)
> - **512, 256, 128**: Dimension values
> - **Red dashed lines**: Tile boundaries and current processing indicators
> - **Step 1, 2, 3**: Sequential computation phases
> - **Plus sign (+)**: Accumulation of partial results


Fig. 63 Matmul tiling (mathematical view).

PSUM accumulation effectively combines Step 2 and 3 above into a single TensorE `nki.isa.nc_matmul` instruction. Assuming
we have `x` in the transposed layout in SBUF, visually the above tiled matmul example will have two back-to-back `nki.isa.nc_matmul`
instructions on TensorE:


> **Figure: mm tiling hw**
>
> A hardware-level view of matrix multiplication tiling showing two iterations with Tensor Engine, SBUF, and PSUM components, demonstrating the Overwrite (first iteration) and Accumulate (second iteration) operations.
>
> This diagram shows the hardware-level execution of tiled matrix multiplication across two iterations, illustrating how partial results are accumulated in PSUM.
>
> **Left side (First iteration)**:
> - **Tensor Engine** contains xT_0 (green tile) with dimensions 128(F) x 128(P), representing the transposed x_0 tile
> - **SBUF** contains y_0 (blue tile) with dimensions 512(F) x 128(P), the moving matrix from State Buffer
> - Arrow labeled "Overwrite" points down from these inputs
> - **PSUM** at bottom contains output_0 (purple tile) with dimensions 512(F) x 128(P)
> - This is the first partial result, written fresh to PSUM
>
> **Right side (Second iteration)**:
> - **Tensor Engine** contains xT_1 (green tile) with dimensions 128(F) x 128(P), the transposed x_1 tile
> - **SBUF** contains y_1 (blue tile) with dimensions 512(F) x 128(P), the next moving matrix
> - Arrow labeled "Accumulate" points down
> - **PSUM** at bottom shows "output_0 + output_1" (purple tile), indicating accumulation of partial results into the same PSUM location
>
> Dimension annotations throughout:
> - 128 (F): Free dimension size (128 elements)
> - 128 (P): Partition dimension size (128 partitions)
> - 512 (F): Larger free dimension for the y/output tiles
>
> Red dashed borders on tiles indicate the current data being processed.
>
> **Key Elements:**
> - **Tensor Engine**: Holds stationary matrix (xT_0, xT_1)
> - **SBUF**: State Buffer holding moving matrix (y_0, y_1)
> - **PSUM**: Partial Sum buffer for output accumulation
> - **Overwrite**: First write to PSUM (replaces contents)
> - **Accumulate**: Subsequent writes add to existing PSUM values
> - **128(F), 128(P), 512(F)**: Dimension annotations
> - **output_0 + output_1**: Shows partial result accumulation


Fig. 64 Matmul tiling (hardware view).

Effectively, the first `nki.isa.nc_matmul` instruction overwrites the destination PSUM bank with the instruction output.
The second instruction accumulates instruction output onto the previous instruction’s result in the same PSUM. The PSUM
accumulation is always done in FP32. A series of TensorE matmul instructions with the first one writing to a PSUM bank and
more subsequent instructions accumulating into the same PSUM bank data is called a *matmul accumulation group*.

In current release of NKI, the `nki.isa.nc_matmul` does not have an explicit
control field to indicate `overwrite` or `accumulate` for
the PSUM. Instead, NeuronCompiler relies on the following NKI code pattern to trigger PSUM accumulation:


```python
# condition 1: a psum buffer with zeros
psum_buf = nl.zeros(..., buffer=nl.psum)

# condition 2: a loop over the contraction dimension
for i in range(N):
   # condition 3: add matmul results from TensorEngine
   psum_buf += nl.matmul(stationary_tile, moving_tile) # or nisa.nc_matmul
```


Refer to the
[Tiling Matrix Multiplications](../programming/tutorials/matrix_multiplication.md#tutorial-matmul-tiling)
tutorial for a detailed implementation.

> **Note**
>
> Note
> 
> 
> Due to current limitations in NKI, `psum_buf[...] = psum_buf + nisa.nc_matmul(stationary_tile, moving_tile)`
> will not reliably trigger the PSUM accumulation architecture feature. Therefore, even though this alternative
> syntax is functionally equivalent to the use of `+=`, it may get lowered to nisa.tensor_tensor on VectorEngine for
> accumulation instead, leading to much lower performance.

Finally, with 8 PSUM banks per partition, TensorE can have up to eight outstanding matmul accumulation groups, which allows
flexible scheduling of matmul instructions on TensorE. Also, the extra buffering from multiple PSUM banks allows us to pipeline
TensorE computation with other compute engines: TensorE can move onto the next accumulation group without waiting for VectorE/ScalarE
to evict previous accumulation group results.