# Introduction to Direct Memory Access (DMA) with NKI

Introduction to Direct Memory Access (DMA) with NKI
Direct Memory Access (DMA) engines in Neuron enable efficient data movement between different memory types, primarily between the device memory (HBM) and on-chip SRAM buffers (SBUF). DMA Engines can operate in parallel to compute, allowing asynchronous data movement independent from compute operations. Each NeuronCore (v2-v4) is paired with 16 DMA engines. Understanding and efficiently utilizing these DMA engines is critical for maximizing memory bandwidth utilization and overall workload performance.

Before reading this doc, it may be helpful to refer to [Introduction to memory hierarchy in NKI](memory-hierarchy-overview.md).

## Basic DMA Capabilities

To move data between HBM and SBUF, programmers can initiate a DMA transfer that gets executed by the DMA engines. Each DMA transfer starts with a DMA trigger from a NeuronCore and ends with a semaphore update from the DMA engine to signal the completion of transfer back to the NeuronCore. Today, each DMA transfer is by default parallelized up to 16 DMA engines, depending on the shape.

The 16 DMA Engines are connected to both the off chip HBM and the on-chip SRAM, called SBUF. DMA transfers can move data in multiple directions: bidirectionally between HBM to SBUF, within HBM or within SBUF. Each DMA engine has a theoretical bandwidth of 27.2 GB/s for NeuronCore-v2 and -v3 or 38.4 GB/s for NeuronCore-v4. DMA engines also support scatter-gather operations, allowing a single transfer to gather data from multiple non-contiguous source buffers or scatter to multiple non-contiguous destination buffers.

DMA transfers can perform both copy and transpose transfers into SBUF. This doc will mainly focus on copy transfers.
You can also perform casting as part of DMA when the transfer has a different source and destination datatype. Neuron supported datatypes can be found in the [NKI datatype guide](api/nki.api.shared.md). The casting operation is performed by first casting the source type to FP32, before finally casting to the destination type. This may be worth considering if working with integer types. Casting with DMAs is not supported for MXFP4 and MXFP8 datatypes.

## DMA Triggers

DMA transfers can be triggered by any engine sequencer in the NeuronCore. (For details, refer to /nki/about/trainium2_arch.) The sequencer instruction to trigger the transfer may wait on any semaphore condition which is signaled by other compute engines to respect data dependencies. The Trigger Engine for a given transfer can be specified by setting the `engine` parameter when calling [nisa.dma_copy](api/api-nki-isa-memory.md#nki-isa-dma_copy). This behavior is only allowed when using hardware DGE.

## DMA Queues

DMA transfers are submitted to DMA queues for the DMA Engines to consume. There are 16 DMA queues per DMA engine (ID 0-15). A given DMA transfer can be submitted to a single queue ID across all 16x DMA engines paired with a NeuronCore. The given queue for a DMA transfer can be seen when mousing over a DMA transfer in a profile in Neuron Explorer. The queue ID is typically tied to the trigger engine and the method of descriptor generation (refer to the NeuronCore-v3 architecture guide for details). DMA transfers within a queue on the same DMA engine are executed in order. DMA transfers from different DMA queues are scheduled in a round robin fashion (for NeuronCore-v2 and v3) or based on the queue QoS configured (for NeuronCore-v4). Refer to the NeuronCore-v4 architecture guide for more details on DMA QoS.

## Performance Considerations

When moving data in or out of SBUF, optimal performance is achieved with transfers maximizing the number of partitions with 4KiB or larger per partition. Given 16x DMA engines and 128 SBUF partitions, each DMA engine is typically responsible for moving data for eight SBUF partitions (128 partitions / 16 DMA engines). The figure below visualizes the DMA throughput across different number of bytes per partition (“Free Bytes”), for a fixed partition dimension size of 128:

!
> **Figure: nki dma intro 1**
>
> A line graph showing DMA throughput in GB/s as a function of bytes per partition, demonstrating performance scaling characteristics when the partition dimension (p_dim) is fixed at 128.
>
> This performance benchmark chart displays the relationship between DMA throughput and data transfer size per partition on NeuronCore hardware. The graph has a characteristic S-curve (sigmoid) shape, illustrating how throughput increases with larger transfer sizes before plateauing at the hardware's maximum bandwidth.
>
> The X-axis represents "Bytes per partition" with data points at powers of 2: 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, and 32768 bytes. The Y-axis shows "DMA Throughput (GB/s)" with a grid background for reference.
>
> The curve shows three distinct regions:
> 1. **Low throughput region** (32-128 bytes): Throughput remains relatively flat and low, indicating overhead-dominated transfers
> 2. **Rapid scaling region** (256-2048 bytes): Throughput increases steeply, showing efficient bandwidth utilization as transfer sizes grow
> 3. **Saturation region** (4096-32768 bytes): Throughput plateaus near the maximum achievable bandwidth, with diminishing returns for larger transfers
>
> Each data point is marked with a green circle and labeled with the corresponding bytes-per-partition value. The title indicates this test was conducted with p_dim (partition dimension) fixed at 128.
>
> **Key Elements:**
> - **Title**: "DMA Throughput varying Bytes per Partition for p_dim = 128"
> - **X-axis**: Bytes per partition (32 to 32768, powers of 2)
> - **Y-axis**: DMA Throughput (GB/s)
> - **Curve shape**: S-curve showing overhead-limited, scaling, and bandwidth-saturated regions
> - **Data points**: 11 measurements from 32 to 32768 bytes
> - **Key insight**: Throughput saturates around 4096+ bytes per partition


The points on the graph refer to various Free (Dimension) Byte values (that is, bytes per partition). We see that at 4096 free bytes, we are able to nearly saturate DMA bandwidth.

Another key consideration for performance is overhead to initiate a DMA transfer. Small, frequent transfers incur significant overhead causing us to be latency bound, while larger transfers help amortize these costs, moving to a more bandwidth bound regime. For optimal performance, it’s important to batch data movements into larger transfers whenever possible.

We will look at two examples below, which show various shapes, sizes and access patterns, and how this affects the the achieved DMA throughput of the corresponding DMA transfers.

## Examples

As DMAs are a result of the corresponding source layout and access pattern, it is best to look at concrete examples to ground our understanding of common applications and their resulting access patterns.

### Example 1: Move A[4,4096] HBM → SBUF

The purpose of this example is to show a very simple access pattern (a 2D tensor in contiguous memory in HBM, being written to SBUF). This should build a foundation of how a particular access pattern maps to a specific set of DMA transfers.

Consider a 2D Tensor, A[4, 4096], in HBM. Assume the tensor is laid out in row-major form and is contiguous in the HBM. In row major form, array elements are stored sequentially row by row in memory, meaning all elements of the first row are stored first, followed by all elements of the second row, and so on. Let’s assume we wish to move this tensor to SBUF, where the destination tensor will have a partition dimension of 4 and a free dimension of 4096. Each row of the source tensor will occupy a single partition in SBUF.

Assuming A is a bfloat16 tensor, this means that the total size of the tensor is 32KiB (4*4096*2B). Knowing that each DMA engine corresponds to 8 partition lanes, and we are writing our 4 rows to only 4 partition lanes of SBUF, we would expect to see a single DMA engine active, with a single transfer size of 32KiB.

Here is a diagram with the expected behavior:

![Diagram showing DMA transfer of A[4,4096] from HBM to SBUF](../../../_images/nki-dma-intro-2.jpg)

#### Example

Here is the kernel to perform the DMA transfer.


```python
import nki.language as nl
import nki.isa as nisa
import nki
import os

os.environ["NEURON_FRAMEWORK_DEBUG"] = "1"
os.environ["NEURON_RT_ENABLE_DGE_NOTIFICATIONS"] = "1"
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"

@nki.jit
def tensor_exp_kernel_isa(in_tensor):
  """NKI kernel to compute elementwise exponential of an input tensor
  Args:
       in_tensor: an input tensor of shape [4,4096]
  Returns:
       out_tensor: an output tensor of shape [4,4096]
  """
  out_tensor = hbm.view(dtype="bfloat16", shape=in_tensor.shape)
  sbuf_tensor =  sbuf.view(dtype="bfloat16", shape=in_tensor.shape)
  out_tile =  sbuf.view(dtype="bfloat16", shape=in_tensor.shape)

  # Load input data from HBM to on-chip memory
  nisa.dma_copy(src=in_tensor[0:4, 0:4096], dst=sbuf_tensor)

  # perform the computation:
  out_tile = nisa.activation(op=nl.exp, data=sbuf_tensor)

  # store the results back to HBM
  nisa.dma_copy(src=out_tile, dst=out_tensor[0:4, 0:4096])
  return out_tensor

if __name__ == "__main__":
  import torch
  from torch_xla.core import xla_model as xm
  device = xm.xla_device()
  shape = (4,4096) # Tensor shape : [4,4096]
  in_tensor = torch.ones(shape,  dtype=torch.bfloat16).to(device=device)
  out_tensor = tensor_exp_kernel_isa(in_tensor)
  print(out_tensor) # an implicit XLA barrier/mark-step
```


#### Profile

The above code runs on a single NeuronCore-v3, in a Trn2 instance. Here we can look at the profile, to validate the expected behavior. Refer to the [Neuron Explorer user guide](api/index.md) for guidance on how to generate a profile.

!
> **Figure: nki dma intro 3**
>
> A Neuron profiler trace screenshot showing DMA load and store operations with annotated details about operation duration, semaphore IDs, and expected transfer sizes.
>
> This is a dark-themed profiler interface screenshot displaying a timeline trace of DMA operations on NeuronCore. The trace visualization shows the execution timeline with multiple tracks for different operation types.
>
> At the top of the trace, two highlighted regions are visible: "Load Operation" on the left side (earlier in time) and "Store Operation" on the right side (later in time), both outlined with red/orange borders for emphasis.
>
> A detailed popup annotation box appears near the Load Operation, containing key profiling information including:
> - DMA Operation Duration
> - Semaphore ID for the DMA Transfer
> - Expected 32KB write in a single transfer
>
> The middle section shows "Semaphore Updates" labels on both the left and right sides of the timeline, indicating synchronization points in the DMA operations.
>
> The bottom portion of the interface displays several horizontal tracks showing timing information, with color-coded bars indicating active operations. A timeline scale at the very bottom shows the time progression from left to right (showing values like 21,000, 21,500, etc.).
>
> The dark background with contrasting colored elements (red/orange highlights, blue annotation boxes) makes it easy to identify the key DMA events and their relationships in the execution timeline.
>
> **Key Elements:**
> - **Load Operation**: First DMA operation (highlighted on left)
> - **Store Operation**: Second DMA operation (highlighted on right)
> - **DMA Operation Duration**: Time taken for the transfer
> - **Semaphore ID**: Synchronization identifier for the DMA transfer
> - **32KB transfer**: Expected single transfer size
> - **Semaphore Updates**: Synchronization points shown in timeline
> - **Timeline tracks**: Multiple horizontal tracks showing operation timing
> - **Time scale**: Bottom axis showing execution time progression


This is exactly what we expected based on our analysis. From the profile, we can see that the first DMA engine takes 1416 ns to load 32 KiB from HBM to SBUF and also a small 4B semaphore update. Even though the remaining 15 DMA engines do not perform useful data movement, they also perform a small 4B semaphore update writes. This allows the NeuronCore to always monitor a semaphore increment of 16 to signal DMA transfer completion, regardless of the tensor shapes in the transfer.

This is good, but this example only uses a single DMA engine. In the next example, we increase partition dimension to increase the number of DMA Engines in use.

### Example 2: Move A[128,128] HBM → SBUF

The purpose of this example is to show how as partition count scales, the number of DMA Engines in use increases.

Consider a 2D Tensor A[128, 128] in HBM, laid out in row-major form and contiguous on the HBM. Assuming we wish to move A from HBM to SBUF, how many DMA engines will this require?

Again, we see the total tensor size is 32KiB (128*128*2B), the same as the previous example. We are writing across 128 partitions of SBUF, with each row corresponding to a partition lane. Knowing that each DMA engine corresponds to 8 partition lanes, and we are writing to 128 partitions, we would expect all 16 DMA engines to be active, each performing a single DMA operation of 2KiB (8 rows x 128 elements x 2 bytes per element).

Here is a diagram of the expected transfer:

![Diagram showing DMA transfer of A[128,128] from HBM to SBUF](../../../_images/nki-dma-intro-4.jpg)

#### Example


```python
import nki.language as nl
import nki.isa as nisa
import nki
import os
os.environ["NEURON_FRAMEWORK_DEBUG"] = "1"
os.environ["NEURON_RT_ENABLE_DGE_NOTIFICATIONS"] = "1"
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"


@nki.jit
def tensor_exp_kernel_isa(in_tensor):
  """NKI kernel to compute elementwise exponential of an input tensor
  Args:
        in_tensor: an input tensor of shape [128,128]
  Returns:
        out_tensor: an output tensor of shape [128,128]
  """
  out_tensor = hbm.view(dtype="bfloat16", shape=in_tensor.shape)
  sbuf_tensor =  sbuf.view(dtype="bfloat16", shape=in_tensor.shape)
  out_tile =  sbuf.view(dtype="bfloat16", shape=in_tensor.shape)

  # Load input data from HBM to on-chip memory
  nisa.dma_copy(src=in_tensor[0:128, 0:128], dst=sbuf_tensor)

  # perform the computation:
  out_tile = nisa.activation(op=nl.exp, data=sbuf_tensor)

  # store the results back to HBM
  nisa.dma_copy(src=out_tile, dst=out_tensor[0:128, 0:128])
  return out_tensor

if __name__ == "__main__":
  import torch
  import torch_xla


  device = torch_xla.device()
  shape = (128, 128) # Tensor shape : [128, 128]
  in_tensor = torch.ones(shape,  dtype=torch.bfloat16).to(device=device)
  print(in_tensor.dtype)
  out_tensor = tensor_exp_kernel_isa(in_tensor)
  print(out_tensor) # an implicit XLA barrier/mark-step
```


#### Profile

!
> **Figure: nki dma intro 5**
>
> A Neuron profiler trace screenshot showing parallel DMA operations across multiple DMA engines, with annotated details highlighting operation duration, semaphore IDs, and transfer sizes.
>
> This dark-themed profiler interface displays a comprehensive timeline view of DMA engine activity across multiple parallel channels. The interface shows individual track rows for each DMA engine along with other NeuronCore components.
>
> On the left side, track labels identify each component:
> - DMA-E64(nc0) through DMA-E79(nc0): 16 DMA engine tracks for NeuronCore 0
> - Scalar(nc0): Scalar engine track
> - GpSimd(nc0): GPSIMD engine track
> - State Buffer (Heap)(nc0): State buffer track
> - Semaphore(nc0): Semaphore synchronization track
> - Pending_DMA_Count(nc0): DMA queue depth track
>
> Two regions are highlighted with red/orange borders:
> 1. "Load Operation and Semaphore Update" (left side): Shows a staggered pattern of DMA load operations across engines E64-E79, with each engine's operation appearing as a small horizontal bar at slightly different times, creating a diagonal pattern.
>
> 2. "Store Operation and Semaphore Update" (right side): Shows the corresponding store operations, also with a staggered pattern across the DMA engines.
>
> A detailed popup annotation near the top provides specific operation details:
> - Time: 34,051ns - 34,148ns
> - Duration: 97ns
> - Semaphore ID: 518 (cpSimd[Dynamic])
> - DMA Engine: 64(nc0)
> - DMA Queue: 0
> - DMA ID: 21
> - Variable: input0
> - Read Size: 2KiB
> - Write Size: 2KiB
> - Transfer Size: 2KiB
>
> Three callout annotations point to key information: "DMA Operation Duration", "Semaphore ID of the DMA Transfer", and "Expected 2KiB transfer size per transfer".
>
> The timeline scale at the bottom shows time from approximately 32,667ns to 38,685ns.
>
> **Key Elements:**
> - **DMA-E64 to DMA-E79**: 16 parallel DMA engine tracks
> - **Load Operations**: Left region showing parallel loads with staggered timing
> - **Store Operations**: Right region showing parallel stores
> - **97ns duration**: Single DMA operation time
> - **2KiB transfer size**: Size of each individual transfer
> - **Semaphore ID 518**: Synchronization identifier
> - **Staggered pattern**: Visual representation of parallel DMA scheduling
> - **Scalar, GpSimd, State Buffer tracks**: Additional NeuronCore component timing


In the above profile, we can see that all 16 DMA engines are active, as each DMA engine is reading 8 rows from HBM and writing to 8 corresponding partition lanes in SBUF. Similarly, we see the reverse also applies from SBUF, back to HBM. By mousing over an individual DMA operation, we see each DMA engine corresponds to a single 2KiB read (8 rows x 128 elements x 2B), as we expect!

Using the same profile from the 128x128 DMA example, lets look at the DMA Trigger and the associated Transfer. You can trace the DMA trigger instruction and the associated DMA transfer via the profiler. This would be useful if you wanted to understand the why a DMA was triggered when, and any preceding dependencies.

!
> **Figure: nki dma intro 6**
>
> A Neuron profiler trace screenshot showing detailed DMA instruction information with a popup displaying semaphore settings, memory patterns, timing data, and source code location.
>
> This dark-themed profiler interface displays a timeline trace with multiple component tracks and a detailed information popup for a DMA operation. The view shows system-level profiling data including cumulative HBM throughput.
>
> The track labels on the left show:
> - qpSimdDynamic (nc0): GPSIMD dynamic operations
> - Scalar(nc0): Scalar engine activity
> - GpSimd(nc0): GPSIMD engine activity
> - State Buffer Usage(nc0): State buffer utilization
> - Semaphore(nc0): Semaphore synchronization events
> - Pending_DMA_Count(nc0): DMA queue depth
> - Cumulative_HBM_Throughput: Aggregate memory bandwidth
> - HBM Throughput: Instantaneous memory bandwidth
> - Cml.Cumulative_DMA_Throughput: Cumulative DMA throughput
> - DMA Throughput (nc0): Per-core DMA throughput
>
> A red annotation arrow points to "DMA Trigger" in the trace, indicating the start of a DMA operation.
>
> The detailed popup (purple/lavender background) displays comprehensive instruction information:
> - Name: semaphore=8 sem_increment=16 src_elem_size=256
> - dst_elem_size=256 src_pattern=[256,1][128,1] dst_pattern=[262144,1][128,1]
> - src_table_offset_imm=0x8 src_table_index=0
> - src_shape_reg=0 dst_addr_imm=0x80200003fe00 dst_shape_reg=0
> - compute_qprnCRQ
> - Time: 33,300 ns - 33,300 ns
> - Duration: 0 ns
> - Opcode: DMA_DIRECT2D
> - Hierarchy: custom=all,1
> - Instruction_Type: REGULAR
> - Compiler PC: 2
> - NKI Source Location: /home/ubuntu/dma-ubenchmarks/intro_to_dma/128x128/128x128.py:23
> - Penguin ID: sy00003
> - Bir Instruction Name: I-9-0
> - Bir ID: sy00d0:28
>
> **Key Elements:**
> - **DMA Trigger**: Annotation showing DMA operation start point
> - **DMA_DIRECT2D opcode**: Direct 2D DMA transfer instruction
> - **Source/Destination patterns**: Memory access patterns for the transfer
> - **Semaphore configuration**: semaphore=8, sem_increment=16
> - **NKI Source Location**: Python file path and line number (line 23)
> - **Penguin/Bir IDs**: Internal compiler instruction identifiers
> - **Duration: 0 ns**: Trigger event (not the full transfer time)
> - **HBM Throughput track**: Memory bandwidth visualization


!
> **Figure: nki dma intro 7**
>
> A Neuron profiler trace screenshot showing a detailed DMA operation popup with timing, semaphore ID, DMA queue assignment, and transfer size information for a 32 KiB data transfer.
>
> This dark-themed profiler interface displays a timeline view focused on a specific DMA operation with a detailed information popup. The trace shows various NeuronCore component tracks alongside throughput metrics.
>
> The track labels on the left include:
> - qpSimdDynamic (nc0): GPSIMD dynamic operations track
> - Scalar(nc0): Scalar engine track
> - GpSimd(nc0): GPSIMD engine track (shows a highlighted purple bar indicating the selected operation)
> - State Buffer Usage(nc0): State buffer utilization track
> - Semaphore(nc0): Semaphore events track
> - Pending_DMA_Count(nc0): DMA queue depth track
> - Cumulative_HBM_Throughput: Aggregate HBM bandwidth track
> - HBM Throughput: Instantaneous HBM bandwidth track
> - Cml.Cumulative_DMA_Throughput: Cumulative DMA throughput
> - DMA Throughput (nc0): Per-core DMA throughput
>
> The detailed popup (purple/lavender background) shows:
> - Time: 34,039 ns - 34,507 ns
> - Duration: 468 ns
> - Semaphore ID: 518 (qpSimdDynamic) - with annotation arrow labeled "Semaphore ID"
> - DMA Queue: qpSimdDynamic - with annotation arrow labeled "DMA Queue"
> - DMA ID: 31637001727072000
> - Variable: input0
> - Read Size: 32 KiB
> - Write Size: 32 KiB
> - Transfer Size: 32 KiB
>
> Two red annotation arrows point from the popup to labels "Semaphore ID" and "DMA Queue" on the right, highlighting these key configuration parameters.
>
> The DMA Throughput track at the bottom shows activity spikes corresponding to the data transfer periods.
>
> **Key Elements:**
> - **Duration: 468 ns**: Time taken for the DMA operation
> - **Semaphore ID: 518**: Synchronization identifier (qpSimdDynamic)
> - **DMA Queue: qpSimdDynamic**: Queue assignment for the transfer
> - **Transfer Size: 32 KiB**: Total data transferred (Read/Write both 32 KiB)
> - **Variable: input0**: Source tensor name
> - **GpSimd track**: Shows active operation highlighted in purple
> - **DMA Throughput**: Bottom track showing bandwidth utilization
> - **Time range**: 32,836 ns to 38,685 ns visible on timeline


We can see the first DMA is triggered from qGpSimdDynamic (First screenshot). We can look at GPSimd to see the corresponding trigger (second screenshot).