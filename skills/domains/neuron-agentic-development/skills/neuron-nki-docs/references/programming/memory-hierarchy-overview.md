# The Trainium Memory Hierarchy

The Trainium Memory Hierarchy
This topic covers the Trainium Memory Hierarchy and how it applies to developing with the AWS Neuron SDK. This overview covers the various memories
that are available on the Trainium hardware and how they are used. Understanding the memory hierarchy is important for writing performant kernels
for use in your Machine Leaning models.

## Memory hierarchy

The diagram in [Fig. 19](#nki-fig-pm-memory), below, shows the four-level memory hierarchy available to a single NeuronCore. The latency
ranges provided in the figure are approximate and are intended to calibrate the programmer’s mental model (see [NeuronDevice Architecture Guide](../architecture/trainium_inferentia2_arch.md) for the exact values). Memories closer to the top of the figure are the closer to the compute engines; therefore, they are designed to provide the highest bandwidth and lowest latency. However, the faster memories also have smaller capacities compared to memories near the bottom. This set of memories is the *Memory Hierarchy* for the Trainium devices.

Unlike memory hierarchies for traditional processors (such as CPUs and GPUs), all of the memories available to a NeuronCore are software-managed. This means the contents of the memories are managed either directly by the programmer, or by the Neuron SDK tool chain, rather than being managed by the hardware. In other words, NeuronCore does not have a hardware cache system that performs data movement across memories in a way that is opaque to the program. All memory movement is explicit in the program itself. These explicit memory movements may be specified by writing a NKI kernel, or they may be computed by the Neuron Graph Compiler as part of the optimization process.

In the following section we will discuss each memory in turn.


> **Figure: pm memory**
>
> A pyramid-shaped memory hierarchy diagram showing the four levels of memory in the Neuron system, from fastest/smallest (PSUM) at top to slowest/largest (Host CPU DRAM) at bottom, with capacity and bandwidth specifications for each level.
>
> This diagram illustrates the complete memory hierarchy for Neuron-based systems, organized as a pyramid with the fastest, smallest memory at the top and progressively larger, slower memory toward the bottom. Color coding and arrows indicate data flow patterns.
>
> **Level 1 - PSUM (Top, Orange/Yellow):**
> - Capacity: ~2 MB
> - Bandwidth: ~10 TB/sec
> - Purpose: Partial sum accumulator for matrix multiplication results
> - Data flow arrows:
>   - Blue "MatMult" arrow pointing up (writing results)
>   - Red "Use MatMult result" arrow pointing down (reading results)
> - Classification: Memory within NeuronCore (on-chip)
>
> **Level 2 - SBUF (Yellow):**
> - Capacity: ~25 MB  
> - Bandwidth: ~10 TB/sec
> - Purpose: State Buffer for operand staging
> - Classification: Memory within NeuronCore (on-chip)
> - Both PSUM and SBUF are bracketed as "Memory within NeuronCore (on-chip)"
>
> **Level 3 - Device Memory HBM (Green):**
> - Capacity: ~50 GB
> - Bandwidth: ~0.5 TB/sec per NC (NeuronCore)
> - Purpose: High Bandwidth Memory for device-level storage
> - Data flow arrows:
>   - Blue "Refill, or Start NKI kernel" arrow pointing up (loading data)
>   - Red "Spill, or End NKI kernel" arrow pointing down (storing data)
> - Classification: Memory within Neuron Device
>
> **Level 4 - Host CPU Memory DRAM (Bottom, Blue):**
> - Capacity: ~1 TB
> - Bandwidth: ~16 GB/sec
> - Purpose: System memory for host CPU
> - Data flow arrows:
>   - Blue "Start compute graph" arrow pointing up
>   - Red "End compute graph" arrow pointing down
>
> **Right Side Annotations:**
> - "Memory within NeuronCore (on-chip)" - brackets PSUM and SBUF
> - "Memory within Neuron Device" - brackets HBM
> - Implicit: Host memory is outside Neuron Device
>
> **Key Bandwidth Insights:**
> - 625x bandwidth difference between on-chip SBUF (~10 TB/s) and HBM (~16 GB/s effective)
> - On-chip memory is precious but extremely fast
> - HBM provides large capacity but requires careful data staging
>
> **Key Elements:**
> - **PSUM (~2 MB, ~10 TB/s)**: Fastest, for matmul accumulation
> - **SBUF (~25 MB, ~10 TB/s)**: On-chip operand storage
> - **HBM (~50 GB, ~0.5 TB/s)**: Device memory, requires DMA
> - **Host DRAM (~1 TB, ~16 GB/s)**: Slowest, largest capacity
> - **Blue arrows**: Data loading/input flow
> - **Red arrows**: Data storing/output flow
> - **Pyramid shape**: Visualizes capacity/speed tradeoff


Fig. 19 NeuronCore Memory Hierarchy with Capacity and Bandwidth Ranges

### NeuronCore external memory

The two memories at the bottom of the hierarchy, host memory and device memory,
are both considered *external* memory for a NeuronCore. These memories are
**linear memory**, where multi-dimensional tensors must be stored in a
flattened manner.

The **host memory** is the CPU-attached DRAM, which is accessible by the host
CPUs and all the NeuronCores attached to the instance. NKI kernels currently do
not provide APIs to move data in and out of the host memory directly, but
rather, rely on ML frameworks such as PyTorch or JAX to send input data from
host memory to the NeuronDevice and vice versa. For an example of this, see
Getting Started with NKI.

The **device memory** resides within a NeuronDevice and uses High Bandwidth
Memory (HBM) technologies starting from NeuronDevice v2. Currently, the input
and output parameters to NKI kernels must be HBM tensor references. When a NKI
kernel begins execution, the first task is to load the input tensors from HBM
into the internal memory. Then computation can be done on the tensors in
internal memory. Once the computation is complete, the results are copied from
the internal memory back to the HBM.

### NeuronCore internal memory

The two memories at the top of the hierarchy, SBUF and PSUM, are both
considered *internal* (or *on-chip*) memory for a NeuronCore. Both memories are
**two-dimensional** memory, organized in **128 partitions**. The partitions
size of PSUM is typically much smaller than SBUF, and PSUM/SBUF partition sizes
vary with NeuronCore generations.

State Buffer (SBUF) memory is the main software-managed on-chip memory. The
SBUF is accessible by all the compute engines within a NeuronCore. NKI kernel
input tensors from HBM must be loaded into the SBUF for computation computed
output tensors of the kernel must be stored back into the HBM from SBUF before
the host can access them.

Both loading and storing to and from the HBM memory can be done using the [nki.isa.dma_copy](api/api-nki-isa-memory.md#nki-isa-dma_copy) API. In addition, SBUF is used for storing intermediate data within the kernel, generated by the compute engines. Note, SBUF has **~20x higher bandwidth** than HBM, but it needs to be carefully managed to minimize HBM accesses for better performance.

Lastly, Partial Sum Buffer (PSUM) memory is a small, dedicated memory designed
for storing matrix multiplication (MatMult) results computed by the tensor
engine. Tensor Engine is able to read-add-write to every address in PSUM.
Therefore, PSUM is useful for performing large MatMult calculations using
multiple tiles where multiple MatMult instructions need to accumulate into the
same output tile. As is shown in `Fig. %s`, PSUM memory
can also be read and written by the vector and scalar engines. However, due to
the limited capacity of PSUM, we recommend that you reserve PSUM space for the
tensor engine to write MatMult outputs and to use the vector and scalar engines
to evict MatMult results back to SBUF as soon as possible.

> **Note**
>
> Note
> 
> 
> To optimize kernel performance, it is good practice for NKI programmers to be mindful of SBUF and PSUM usage through careful [tiling](tiling-overview.md#nki-about-tiling) and loop fusion. If the total size of the live data being used by a NKI kernel overflows the capacity of any on-chip memory, the Neuron compiler will insert the necessary spills or refills between that memory and the next-tier memory in the hierarchy.