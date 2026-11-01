# Fused Mamba

> **NOTE:** This tutorial contains code examples using deprecated Beta 1 patterns (`nl.load`, `nl.store`).
> For migration guidance, see the [NKI Migration Guide](../../reference/migration/nki-migration-guide.md) (Beta 1 to Beta 2)
> and the [NKI 0.3.0 Update Guide](../../reference/migration/nki-030-update-guide.md) (Beta 2 to GA).
> Key changes needed: Replace `nl.load`/`nl.store` with `nisa.dma_copy` and add explicit tile allocations.

Fused Mamba
In this tutorial, we implement a NKI kernel for the [Mamba Large Language Model](https://arxiv.org/abs/2312.00752),
a State Space Model (SSM) which replaces
the attention of a regular Transformer model with a custom layer inspired by Recurrent Neural Networks. We will walk through
the core computation step-by-step and map it to NKI APIs to form a functional kernel. Next, by scaling the input shapes
of the kernel (both channel size and sequence length), we will iterate on a more hardware-efficient kernel implementation
to improve the scaling efficiency.

In this tutorial, we learn about:

* Mapping different vector operations efficiently to NeuronCore compute engines, such as associative scan and element-wise
operations between tensors

* Leveraging data reuse and tiling to reduce excessive data movement and keep compute engines busy

* Using [neuron-profile](../../optimization/use-neuron-profile.md) to identify performance bottlenecks and opportunities

## PyTorch Reference Implementation

Before jumping to NKI, let’s examine the compute definition of a Mamba-v1 layer using the below PyTorch script
(`mamba_torch.py`):


```python
import torch
import torch_neuronx
import torch_xla.core.xla_model as xm
import os
import argparse

os.environ["NEURON_FRAMEWORK_DEBUG"] = "1"
os.environ["NEURON_CC_FLAGS"]= " --model-type=transformer --disable-dge "


def associative_scan(deltaA, deltaB_u):
    """
    Args:
        deltaA: [batch_size, channels, state_size, seq_len]
        deltaB_u: [batch_size, channels, state_size, seq_len]

    Mamba uses an associative scan operator to aggregate information across
    time sequentially (sequence length, e.g. sequence of tokens),
    from the past to the present.
    """
    batch_size, channels, state_size, seq_len = deltaA.shape
    out = torch.empty(batch_size, channels, state_size, seq_len,
                        device=deltaA.device, dtype=deltaA.dtype)
    for i in range(seq_len):
        prev_state = out[..., i - 1] if i > 0 else 0
        out[..., i] = deltaA[..., i] * prev_state + deltaB_u[..., i]
    return out


def mamba_layer(delta, A, B, u, C):
    """
    Args:
        delta: [batch, channels, seq_len]
        u: [batch, channels, seq_len]
        A: [channels, state_size]
        B: [batch, state_size, seq_len]
        C: [batch, state_size, seq_len]
    """
    # expand the tensors so they all have the same dimensions and compute elementwise products (with broadcast)
    # deltaA and deltaB_u have shape [batch_size, channels, state_size, seq_len]
    deltaA = torch.exp(delta[:, :, None, :] * A[None, :, :, None])
    deltaB_u = delta[:, :, None, :] * B[:, None, :, :] * u[:, :, None, :]
    scan_res = associative_scan(deltaA, deltaB_u)
    # y sums over the `state_size` axis and has shape [batch_size, channels, seq_len]
    mamba_out = (C[:, None, :, :] * scan_res).sum(dim=-2)
    return mamba_out


def parse_args():
    parser = argparse.ArgumentParser(
    """Run Mamba PyTorch implementation. Hard-coded small example only since
       PyTorch implementation is very slow for larger configs.
    """)
    parser.add_argument("--mode",
                        choices=["accuracy", "perf"],
                        default="accuracy",
                        help="""Do accuracy test or perf test.
                                Accuracy test compares mamba_v1 kernel against PyTorch implementation.
                                Perf test will generate a NEFF for the PyTorch implementation in local directory
                                for a manual run of neuron-profile.
                             """)
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = parse_args()

    # Toy example
    batch = 1
    seq_len = 512
    channels = 256
    state_size = 16

    dtype = torch.float32

    device = xm.xla_device()

    delta = torch.ones(batch, channels, seq_len, dtype=dtype, device=device)
    u = torch.ones(batch, channels, seq_len, dtype=dtype, device=device)

    # For numerical accuracy testing purposes, we choose negative numbers for A on purpose.
    # Otherwise, the associative scan will integrate too fast and overflow, which would
    # mask any real numerical issues in our computation.
    # A negative A will ensure we catch numerical issues when we have them.
    A = -torch.ones(channels, state_size, dtype=dtype, device=device)
    B = torch.ones(batch, state_size, seq_len, dtype=dtype, device=device)

    C = torch.ones(batch, state_size, seq_len, dtype=dtype, device=device)

    xm.mark_step()
    torch_out = mamba_layer(delta, A, B, u, C)
    xm.mark_step()
    print(torch_out)
```


The input tensor shapes are as follows:

* `delta: [batch, channels, seq_len]`

* `u: [batch, channels, seq_len]`

* `A: [channels, state_size]`

* `B: [batch, state_size, seq_len]`

* `C: [batch, state_size, seq_len]`

The key model parameters are:

* `batch`: batch size of the model.

* `seq_len`: sequence length of the model.

* `channels`: hidden size of a token.

* `state_size`: number of model states.

We use `[batch=1, seq_len=512, channels = 256, state_size = 16]` as a simple test case for initial performance evaluation.

Running the above Python script will compile the `PyTorch` compute graph using Neuron Compiler and generate a Neuron executable
file (NEFF) in the same directory. We can then profile the NEFF on a single NeuronCore using [neuron-profiler](../../optimization/use-neuron-profile.md).
Figure below is a screenshot of the profile. We see this initial PyTorch implementation takes **151.83 ms** to execute *on
device*.


> **Figure: mamba torch ref**
>
> A Neuron profiler timeline visualization showing the performance profile of a Mamba torch reference implementation, highlighting that data movement activities significantly dominate over compute activities.
>
> This profiler screenshot displays a horizontal timeline view with multiple activity tracks stacked vertically. At the top, a red annotation box points to dense activity bars with the text "Data movement activities, noticeably more busy than compute activities." The left side shows labels for different hardware components and activities including GPSIMD activities, various engine states, State Buffer Usage, PSUM usage, Pending DMA Count, DMA Throughput, and other metrics.
>
> The timeline spans from approximately 0 to 133+ milliseconds along the horizontal axis. The upper portion shows frequent, densely packed activity bars representing data movement operations. An orange annotation box on the left side labels "Compute activities" which appear sparse compared to the data movement tracks above.
>
> Near the bottom, a blue annotation box highlights "Easily saturated DMA throughput" pointing to the DMA Throughput track. At the very bottom, a purple annotation box spans nearly the entire timeline with the label "Execution time on device" showing the total execution time of approximately 133.43 ms.
>
> **Key Elements:**
> - **Data movement activities (top)**: Dense activity bars showing heavy DMA operations, annotated in red as "noticeably more busy than compute activities"
> - **Compute activities (middle)**: Relatively sparse activity bars labeled in orange, indicating underutilization of compute resources
> - **DMA Throughput track**: Shows sustained high throughput, annotated as "Easily saturated DMA throughput"
> - **Execution time**: Total device execution time of ~133.43 ms highlighted in purple at the bottom
> - **Timeline scale**: Horizontal axis showing time in milliseconds from 0 to over 133 ms
> - **Activity tracks**: Multiple rows showing GPSIMD, Tensor Engine, State Buffer, PSUM, and DMA metrics


Fig. 28 Profile of Mamba PyTorch Implementation

Zooming into a portion of the profile, we notice the compute activities on different engines (TensorE/VectorE/ScalarE/GpSimdE)
are quite sparse compared to data movement activities (the qSyncIO0 and qVectorSpillReload rows):


> **Figure: mamba torch ref zoomed**
>
> A zoomed-in Neuron profiler timeline view of the Mamba torch reference implementation showing an 11.1 ms window that reveals sparse compute activity and detailed memory usage patterns.
>
> This profiler screenshot shows a detailed view of approximately 11.1 milliseconds of execution time, with the horizontal timeline spanning from roughly 24,000,000 to 32,000,000 cycles. The view is organized into multiple horizontal tracks displaying different aspects of hardware activity.
>
> At the top, the qSyncIO0 track shows a continuous yellow/orange bar indicating constant I/O synchronization activity, interspersed with blue vertical marks representing specific events. Below this, qVectorSpillReload0 shows very sparse green marks, indicating occasional vector spill and reload operations.
>
> The compute engine tracks in the middle section reveal the sparse nature of actual computation. The SyncE track displays regular red synchronization marks. TensorE shows sporadic red marks for tensor engine operations. TensorMatrixE displays sparse green marks for matrix tensor operations. VectorE contains scattered red marks for vector engine activities, and ScalarE shows sparse cyan marks for scalar engine operations. The gaps between these marks visually demonstrate significant compute underutilization.
>
> The lower section displays memory metrics. State Buffer Usage maintains a consistent yellow line near the top, indicating steady buffer utilization. PSUM Usage shows a colorful stacked area chart with multiple colors (green, blue, pink, orange, yellow) representing different PSUM allocation patterns that fluctuate over time. Pending DMA Count displays as a dark blue stepped line showing queue depth variations. DMA Throughput appears as a red line near the bottom, and Estimated MFU is shown at the very bottom of the view.
>
> **Key Elements:**
> - **qSyncIO0**: Continuous yellow bar showing constant I/O sync activity at the top
> - **SyncE**: Red vertical marks indicating synchronization operations
> - **TensorE/TensorMatrixE**: Sparse activity marks showing underutilized tensor engine
> - **VectorE/ScalarE**: Infrequent marks indicating low vector and scalar engine usage
> - **State Buffer Usage**: Yellow line showing consistent memory buffer utilization
> - **PSUM Usage**: Multi-colored stacked area chart showing partial sum memory patterns
> - **Pending DMA Count**: Blue stepped line tracking DMA operation queue depth
> - **Timeline scale**: Spans ~11.1 ms window from 24M to 32M cycles


Fig. 29 Profile of Mamba PyTorch Implementation (Zoomed-in)

In this seemingly “memory-bound” execution trace, the achieved DMA throughput is also extremely low, hovering around
0.33% utilization throughout execution. Therefore, we are stressing neither the compute nor the memory subsystem, hinting
the workload is running at low efficiency on the NeuronCore. In the rest of this tutorial, we will showcase how to re-write
the above computation using NKI to achieve a device execution latency of **172.93 usec** , which is a **878x speedup**
compared to the PyTorch reference implementation.

## Mapping Mamba Layer to NeuronCore

In this section, we will discuss how the computation can be mapped onto the NeuronCore architecture. We will also highlight
the importance of choosing appropriate data layouts to achieve good compute efficiency.

Recall we have the following input tensor shapes in device memory:

* `delta: [batch_size, channels, seq_len]`

* `u: [batch_size, channels, seq_len]`

* `A: [channels, state_size]`

* `B: [batch_size, state_size, seq_len]`

* `C: [batch_size, state_size, seq_len]`

In fact, the above tensor layout has been chosen carefully based on the computation done in NeuronCore, which we will discuss
in more detail below.

In Mamba models, both `seq_len` and `channels` are typically in the thousands (such as `seq_len=16K, channels=4K`),
while `batch_size` and `state_size` are much smaller by 2-3 order of magnitudes (such as `batch_size=4, state_size=16`).
To simplify visualization of computation
on multi-dimensional tensors, let’s hold `batch` and `state_size` dimension constant and focus on computation per batch
per state. Note, the `batch_size` dimension is considered a fully parallel axis in a Mamba layer, while `state_size`
is only a partial parallel axis where results from different states will be accumulated together.

By extracting `batch` and `state_size` dimensions, we get the following input tensor shapes in device memory:

* `delta_i: [channels, seq_len]`

* `u_i:&#160;&#160;&#160;&#160; [channels, seq_len]`

* `A_i:&#160;&#160;&#160;&#160; [channels]`

* `B_i:&#160;&#160;&#160;&#160; [seq_len]`

* `C_i:&#160;&#160;&#160;&#160; [seq_len]`

Next, let’s visualize the data flow and computation using 2D matrices or vectors step-by-step.

### Step 1: Element-wise multiplication of `delta_i` and `A_i`

We have the following PyTorch reference code for Step 1:


```python
# delta[batch, channels, seq_len]
# A    [channels, state_size]
delta[:, :, None, :] * A[None, :, :, None]

# Holding batch and state_size constant
# delta_i: [channels, seq_len]
# A_i:     [channels]
delta_i[:, :] * A_i[:]
```


After the above transformation, the multiplication between `delta_i` and `A_i` involves a **broadcasting** across the
`seq_len` dimension of `delta_i`. In NKI, free-dimension broadcast can often be folded into the actual computation instruction
at no additional performance cost, while partition-dim broadcast often requires a separate instruction on TensorE (see TensorE
alternative use case in [Trainium/Inferentia2 Architecture Guide](../../architecture/trainium_inferentia2_arch.md#arch-sec-tensor-engine-alternative-use)).
As a result, we have two options for executing Step 1.

**Option 1: Map ``seq_len`` to free dimension.** Element-wise multiplication of `delta_i` and `A_i` on NeuronCore can
be done through nisa.tensor_scalar
on either VectorE or ScalarE, which automatically broadcast `A_i` along the free dimension to match the `seq_len` dimension
in `A_i`.

Note, the `channels` dimension is mapped to SBUF partition dimension. Since the input `channels` dimension has a size
of 256 in our initial setup, which exceeds the architectural limitation of `nl.tile_size.pmax=128` , we must **tile**
`delta_i` in the `channels` dimension (tiled dimension denoted as `channels_tiled`) and feed one tile into `nisa.tensor_scalar`
at a time. Figure below illustrates the computation done for Option 1.


> **Figure: mamba step1 opt1**
>
> A tensor operation diagram showing the element-wise multiplication of delta_i and A_i tensors to produce deltaA_i, with dimension annotations showing the mapping to NKI partition (P) and free (F) dimensions.
>
> This diagram illustrates the first optimization option for Step 1 in the Mamba kernel implementation, showing a tensor multiplication operation.
>
> Three tensors are displayed from left to right with mathematical operators between them:
>
> On the left, a large green rectangular tensor labeled "delta_i" has dimensions annotated as:
> - Width: seq_len (F dim) - the sequence length mapped to the free dimension
> - Height: channels_tiled (P dim) - the tiled channels mapped to the partition dimension
>
> In the center, a narrow blue vertical tensor labeled "A_i" has dimensions:
> - Width: 1 (F dim) - single element in free dimension
> - Height: channels_tiled (P dim) - matching the partition dimension of delta_i
>
> A multiplication symbol (*) appears between delta_i and A_i.
>
> On the right, a purple rectangular tensor labeled "deltaA_i" shows the result with dimensions:
> - Width: seq_len (F dim) - same as delta_i
> - Height: channels_tiled (P dim) - same as input tensors
>
> An equals sign (=) connects the multiplication to the result.
>
> This layout demonstrates broadcasting: the narrow A_i tensor (with free dimension of 1) is broadcast across the seq_len free dimension of delta_i during the element-wise multiplication.
>
> **Key Elements:**
> - **delta_i**: Green input tensor with shape [channels_tiled, seq_len]
> - **A_i**: Blue input tensor with shape [channels_tiled, 1]
> - **deltaA_i**: Purple output tensor with shape [channels_tiled, seq_len]
> - **seq_len (F dim)**: Sequence length dimension mapped to NKI free dimension
> - **channels_tiled (P dim)**: Tiled channels mapped to NKI partition dimension
> - **Multiplication (*)**: Element-wise multiplication with broadcasting
> - **1 (F dim)**: Single-element free dimension in A_i enabling broadcast


Fig. 30 Step 1, Option 1: nisa.tensor_scalar

As an example, the associated NKI code for batch `i_batch`, state `i_state` and tile `i_tile_channels` in `channels`
is:


```python
# Input shape in device memory matches the computation layout
# Device memory layout:
# delta_i: [channels, seq_len]
# A_i:     [channels]

# Computation layout in SBUF:
# delta_i: [par_dim(channels), seq_len]
# A_i:     [par_dim(channels)]

deltaA_i = nisa.tensor_scalar(delta_i, op0=nl.multiply, operand0=A_i)
```


Note, with this compute layout option, the `delta_i` tensor shape `[channels, seq_len]` in device memory can be loaded
into SBUF efficiently with `seq_len` as the free dimension and fed into VectorE/ScalarE for computation. No extra transposes
are needed.

**Option 2: Map ``seq_len`` to partition dimension.** Alternatively, if we choose a transposed layout for `delta_i` in
SBUF for computation, we will need a partition-dimension broadcast of `A_i` using a separate instruction on TensorE
(`A_i.broadcast_to(...)`) and then a nisa.tensor_tensor
operation between `delta_i` and the broadcast `A_i` on VectorE. As a reminder, we need to tile the `seq_len` dimension
to meet the tile size constraint `nl.tile_size.pmax=128`. Figure below illustrates the computation done for Option 2.


> **Figure: mamba step1 opt2**
>
> A tensor operation diagram showing an alternative layout for Mamba Step 1 where A_i_bcast is transposed with explicit p-dim broadcast annotation, multiplying with delta_i to produce deltaA_i.
>
> This diagram illustrates the second optimization option for Step 1 in the Mamba kernel, showing an alternative tensor layout with transposed dimensions.
>
> Three tensors are displayed from left to right with mathematical operators between them:
>
> On the left, a green rectangular tensor labeled "delta_i" has dimensions annotated as:
> - Width: channels (F dim) - channels mapped to the free dimension
> - Height: seq_len_tiled (P dim) - tiled sequence length mapped to the partition dimension
>
> In the center, a blue horizontal tensor labeled "A_i_bcast" has a different orientation:
> - Width: channels (F dim) - matching the free dimension of delta_i
> - Height: minimal (single row)
> - An annotation "p-dim broadcast" with downward-pointing arrows indicates that this tensor will be broadcast across the partition dimension
>
> A multiplication symbol (*) appears between delta_i and A_i_bcast.
>
> On the right, a purple rectangular tensor labeled "deltaA_i" shows the result with dimensions:
> - Width: channels (F dim) - same as inputs
> - Height: seq_len_tiled (P dim) - same as delta_i
>
> An equals sign (=) connects the multiplication to the result.
>
> This alternative layout transposes the tensor dimensions compared to Option 1, with sequence length now on the partition dimension and channels on the free dimension, requiring broadcast along the partition dimension.
>
> **Key Elements:**
> - **delta_i**: Green input tensor with shape [seq_len_tiled, channels]
> - **A_i_bcast**: Blue input tensor requiring p-dim broadcast
> - **deltaA_i**: Purple output tensor with shape [seq_len_tiled, channels]
> - **channels (F dim)**: Channels mapped to NKI free dimension
> - **seq_len_tiled (P dim)**: Tiled sequence length mapped to NKI partition dimension
> - **p-dim broadcast**: Explicit annotation showing broadcast direction along partition dimension
> - **Downward arrows**: Visual indication of broadcast direction


Fig. 31 Step 1, Option 2: p-dim broadcast + nisa.tensor_tensor

The associated NKI code is as follows:


```python
# Input shape in device memory does NOT match the computation layout
# Device memory layout:
# delta_i: [channels, seq_len]
# A_i:     [channels]

# Computation layout in SBUF:
# delta_i: [par_dim(seq_len_tiled), channels]
# A_i:     [par_dim(1), channels]

A_i_bcast = A_i.broadcast_to((nl.tile_size.pmax, channels))
deltaA_i = nisa.tensor_tensor(delta_i, A_i_bcast, op=ml.multiply)
```


Assuming the same `delta_i` device memory layout `[channels, seq_len]`, before performing the `nisa.tensor_tensor`
instruction, we will need to either:

* Do a regular load of `delta_i` into SBUF using nl.load and an explicit transpose on the loaded `delta_i` using
`nl.transpose` to make `seq_len` lie in the free dimension, or

* Do a transposed load of `delta_i` using nl.load_transpose2d,
which is significantly less efficient in memory bandwidth usage compared to `nl.load`

If Option2 was chosen as the compute layout, we would have incentives to define the `delta` input tensor shape as `[seq_len,
channels]` in device memory instead.

From computation perspectives, Option 2 is less efficient than Option 1 because:

* Option 2 needs an extra TensorE instruction performing partition dimension broadcast.

* `nisa.tensor_tensor` is 2x slower than `nisa.tensor_scalar` for our input data type FP32 (see API doc for instruction
cost estimates).

Therefore, for Step 1 only, Option 1 is the winner compared to Option 2. Let’s continue with the rest of the steps to see
if we need to revise this selection due to surrounding operator layout preferences.

### Step 2: Exponential of deltaA_i.

Step 2 is evaluating exponential on `deltaA_i` from the previous step:


```python
torch.exp(...)
```


In NeuronCore, evaluating an exponential function on a tensor is considered a scalar operation, which runs on ScalarE. This
operation can be invoked through nl.exp
or nisa.activation.
However, ScalarE is able to perform a “pipelined multiply-add” on the input before evaluating a non-linear function (detail
see [Trainium/Inferentia2 Architecture Guide](../../architecture/trainium_inferentia2_arch.md#arch-sec-scalar-pipelined-fma)).
In other words, we can fold Step 1 (Option 1) `nisa.tensor_scalar` and Step 2 into a single ScalarE instruction at
no additional cost. This functionality is only exposed in the `nisa.activation` API. This folding is not feasible if we
chose Option 2 `nisa.tensor_tensor` in Step 1. Figure below illustrates our new execution plan to combine Step 1 and 2
into `nisa.activation` :


> **Figure: mamba step2**
>
> A tensor operation diagram showing the exponential function applied to the product of delta_i and A_i tensors, computing exp(delta_i * A_i) = deltaA_i for the Mamba kernel Step 2.
>
> This diagram illustrates Step 2 of the Mamba kernel implementation, showing the computation of the exponential of the element-wise product.
>
> The equation is presented visually with "exp(" on the far left, followed by tensor representations, and closing with ")" before the equals sign:
>
> On the left (inside the exp function), a green rectangular tensor labeled "delta_i" has dimensions:
> - Width: seq_len (F dim) - sequence length mapped to free dimension
> - Height: channels_tiled (P dim) - tiled channels mapped to partition dimension
>
> A multiplication symbol (*) follows.
>
> In the center, a narrow blue vertical tensor labeled "A_i" has dimensions:
> - Width: 1 (F dim) - single element in free dimension
> - Height: channels_tiled (P dim) - matching the partition dimension
>
> The closing parenthesis of exp() is followed by an equals sign (=).
>
> On the right, a purple rectangular tensor labeled "deltaA_i" shows the final result with dimensions:
> - Width: seq_len (F dim) - same as delta_i
> - Height: channels_tiled (P dim) - same as input tensors
>
> This step takes the result from Step 1 (the element-wise multiplication with broadcasting) and applies the exponential function element-wise, which is essential for the Mamba state space model computation.
>
> **Key Elements:**
> - **exp()**: Exponential function wrapping the multiplication
> - **delta_i**: Green input tensor with shape [channels_tiled, seq_len]
> - **A_i**: Blue input tensor with shape [channels_tiled, 1]
> - **deltaA_i**: Purple output tensor containing exp(delta_i * A_i)
> - **seq_len (F dim)**: Sequence length on free dimension
> - **channels_tiled (P dim)**: Tiled channels on partition dimension
> - **1 (F dim)**: Single-element free dimension enabling broadcast in A_i


Fig. 32 Step 1&2: `nisa.activation`

The associated NKI code is as follows:


```python
# Input shape in device memory matches the computation layout
deltaA_i = nisa.activation(op=nl.exp, data=delta_i, scale=A_i)
```


### Step 3: Element-wise multiplication of delta_i, B_i and u_i.

PyTorch reference code for Step 3 is:


```python
# delta[batch, channels, seq_len]
# B:   [batch, state_size, seq_len]
# u:   [batch, channels, seq_len]
delta[:, :, None, :] * B[:, None, :, :] * u[:, :, None, :]

# Holding batch and state_size constant
# delta_i: [channels, seq_len]
# B_i:     [seq_len]
# u_i:     [channels, seq_len]
delta_i[:, :] * B_i[None, :] * u_i[:, :]
```


This step involves similar compute layout and instruction choices as Step 1:

* `channels` is either partition or free dimension for both `delta_i` and `u_i`

* multiplication with `B_i` is either through `nisa.tensor_tensor` or `nisa.tensor_scalar`

Since we preferred Step 1 to consume `delta_i` using `channels` as the partition dimension in previous steps, it is
wise to follow the same layout choice here for `delta_i` to avoid any transposes. Given this layout choice, the multiplication
with `B_i` will have to be a `nisa.tensor_tensor`. Figure below visualizes the computation in Step 3:


> **Figure: mamba step3**
>
> A two-part tensor operation diagram for Mamba Step 3, showing the computation of deltaU_i from delta_i and u_i (top row), followed by computing deltaBu_i from deltaU_i and B_i with p-dim broadcast (bottom row).
>
> This diagram illustrates Step 3 of the Mamba kernel implementation, consisting of two sequential tensor operations displayed in two rows.
>
> In the top row (first operation):
> - A green tensor labeled "delta_i" with dimensions seqlen (F dim) width and channels_tiled (P dim) height
> - Multiplied (*) by a yellow tensor labeled "u_i" with matching dimensions seqlen (F dim) width and channels_tiled (P dim) height
> - Equals (=) a purple tensor labeled "deltaU_i" with the same dimensions seqlen (F dim) by channels_tiled (P dim)
>
> In the bottom row (second operation):
> - The purple tensor "deltaU_i" from the previous step with dimensions seqlen (F dim) by channels_tiled (P dim)
> - Multiplied (*) by a blue horizontal tensor labeled "B_i" with width seqlen (F dim) but requiring p-dim broadcast (indicated by downward-pointing arrow)
> - Equals (=) a pink/salmon tensor labeled "deltaBu_i" with dimensions seqlen (F dim) by channels_tiled (P dim)
>
> The B_i tensor shows the "p-dim broadcast" annotation with a downward arrow, indicating it is broadcast across the partition dimension to match deltaU_i's shape during the element-wise multiplication.
>
> **Key Elements:**
> - **delta_i**: Green input tensor [channels_tiled x seqlen]
> - **u_i**: Yellow input tensor [channels_tiled x seqlen]
> - **deltaU_i**: Purple intermediate result [channels_tiled x seqlen]
> - **B_i**: Blue tensor requiring p-dim broadcast
> - **deltaBu_i**: Pink/salmon final output [channels_tiled x seqlen]
> - **seqlen (F dim)**: Sequence length mapped to free dimension
> - **channels_tiled (P dim)**: Tiled channels mapped to partition dimension
> - **p-dim broadcast**: Broadcast operation along partition dimension for B_i


Fig. 33 Step 3: p-dim broadcast + 2x `nisa.tensor_tensor`

The associated NKI code is as follows:


```python
# Input shape in device memory does NOT match the computation layout
# Device memory layout:
# delta_i: [channels, seq_len]
# u_i:     [channels, seq_len]
# B_i:     [seq_len]

# Computation layout in SBUF:
# delta_i: [par_dim(channels_tiled), seq_len]
# u_i:     [par_dim(channels_tiled), seq_len]
# B_i:     [par_dim(1), seq_len]

deltaU_i = nisa.tensor_tensor(delta_i, u_i, op=ml.multiply)
B_i_bcast = B_i.broadcast_to((nl.tile_size.pmax, seq_len))
deltaBu_i = nisa.tensor_tensor(deltaU_i, B_i_bcast, op=ml.multiply)
```


### Step 4: Associative scan between deltaA_i and deltaBu_i

In this step, we use an associative scan operator between `deltaA` and `deltaBu` to aggregate information across time
sequentially (sequence length, e.g. sequence of tokens), from the past to the present. Here is a PyTorch reference implementation:


```python
# deltaA:   [batch_size, channels, state_size, seq_len]
# deltaB_u: [batch_size, channels, state_size, seq_len]
out = torch.empty(batch_size, channels, state_size, seq_len,
                  device=deltaA.device, dtype=deltaA.dtype)

for i in range(seq_len):
    # starting state is 0
    prev_state = out[..., i - 1] if i > 0 else 0
    # multiply deltaA by the previous time step state and then add deltaB_u
    out[..., i] = deltaA[..., i] * prev_state + deltaB_u[..., i]
```


By holding batch and state_size dimensions constant, we get `deltaA_i` and `deltaBu_i` both with
`[channels_tiled, seq_len]`, where `channels_tiled` is the partition dimension.
The associative scan between these two tile shapes can
be implemented in NKI naively through the following loop:


```python
scan_i = nl.ndarray((channels_tiled, seq_len), ...)

# Peeling the first iteration out, which is
# equivalent to loop iterator dependent control flow within the loop
scan_i[0:channels_tiled, 0] = deltaBu[0:channels_tiled, 0]

for i in range(seq_len - 1):
   scan_i[0:channels_tiled, i+1] =    deltaA_i[0:channels_tiled, i+1] * scan_i[0:channels_tiled, i]
                                    + deltaBu_i[0:channels_tiled, i+1]
```


Within the loop, the current implementation invokes one instruction for multiplication and another for addition. Since both
instructions are performed among tiles of shape `[channels_tiled, 1]`, we can combine
these two instructions using [nisa.tensor_scalar](../api/api-nki-isa-tensor.md#nki-isa-tensor_scalar)
which supports two operators in a pipelined fashion within an instruction at the same cost as a single operator. Below is
a new implementation that could provide 2x speedup compared to the above:


```python
scan_i = nl.ndarray((channels_tiled, seq_len), dtype=deltaA.dtype, buffer=nl.sbuf)
scan_i[0:channels_tiled, 0] = deltaBu[i_p, 0]

for i in range(seq_len - 1):
   scan_i[0:channels_tiled, i+1] = nisa.tensor_scalar(
        deltaA[0:channels_tiled, i+1],
        op0=nl.multiply,
        operand0=scan_i[0:channels_tiled, i],
        op1=nl.add,
        operand1=deltaBu[0:channels_tiled, i+1])
```


However, the above loop nest will turn into `seq_len` many instructions with input tiles that have a single element per
partition in SBUF. In addition, every `nisa.tensor_scalar` instruction has a data dependency on the output of the previous
instruction. As discussed in the [Trainium/Inferentia2 Architecture Guide](../../architecture/trainium_inferentia2_arch.md#arch-sec-vector-engine-perf),
these two traits combined in the instruction sequence is considered extremely *inefficient* on ScalarE/VectorE, where
the static instruction overhead instead of the useful execution time would be dominating the engine timeline.

Conveniently, NKI exposes another instruction [nisa.tensor_tensor_scan](../api/api-nki-isa-tensor.md#nki-isa-tensor_tensor_scan)
on VectorE, which can perform the above loop nest in a *single* instruction by caching the intermediate scan result from
the previous time step internally in VectorE without going through SBUF.


```python
scan_i = nisa.tensor_tensor_scan(deltaA_i, deltaBu_i, initial=0,
                                 op0=np.multiply, op1=np.add)
```


Note, the shape of `scan_i` is exactly the same as the input `deltaA_i/deltaBu_i`: `[channels_tiled, seq_len]`.

### Step 5: Element-wise multiplication of C_i and scan_i

The PyTorch reference implementation is:


```python
# scan_res: [batch_size, channels, state_size, seq_len]
# C:        [batch_size, state_size, seq_len]
scanC = C[:, None, :, :] * scan_res

# Holding batch and state constant
# scan_i: [channels_tiled, seq_len]
# C_i:    [seq_len]
scanC_i = C_i[None, :] * scan_i[:, :]
```


You know the drill - Since `channels_tiled` is the partition dimension in `scan_i` from the previous step, we need to
perform a partition-dimension broadcast on `C_i` before invoking `nisa.tensor_tensor`:


> **Figure: mamba step5**
>
> A tensor operation diagram for Mamba Step 5, showing the element-wise multiplication of scan_i with C_i (using p-dim broadcast) to produce scanC_i.
>
> This diagram illustrates Step 5 of the Mamba kernel implementation, showing how the scan output is multiplied with the C matrix.
>
> Three tensors are displayed from left to right:
>
> On the left, a green rectangular tensor labeled "scan_i" has dimensions:
> - Width: seqlen (F dim) - sequence length mapped to the free dimension
> - Height: channels_tiled (P dim) - tiled channels mapped to the partition dimension
>
> In the center, a blue horizontal tensor labeled "C_i" has:
> - Width: seqlen (F dim) - matching the free dimension
> - A minimal height requiring broadcast
> - An annotation "p-dim broadcast" with a downward-pointing arrow indicates this tensor will be broadcast across the partition dimension
>
> A multiplication symbol (*) appears between scan_i and C_i.
>
> On the right, a purple rectangular tensor labeled "scanC_i" shows the result with dimensions:
> - Width: seqlen (F dim) - same as inputs
> - Height: channels_tiled (P dim) - same as scan_i
>
> An equals sign (=) connects the multiplication to the result.
>
> This step computes the element-wise product of the scan output with the C matrix, which is essential for computing the final output in the Mamba selective state space model.
>
> **Key Elements:**
> - **scan_i**: Green input tensor from scan operation [channels_tiled x seqlen]
> - **C_i**: Blue tensor requiring p-dim broadcast along partition dimension
> - **scanC_i**: Purple output tensor [channels_tiled x seqlen]
> - **seqlen (F dim)**: Sequence length mapped to free dimension
> - **channels_tiled (P dim)**: Tiled channels mapped to partition dimension
> - **p-dim broadcast**: Downward arrow indicating broadcast along partition dimension


Fig. 34 Step 5: p-dim broadcast + `nisa.tensor_tensor`

The corresponding NKI code is:


```python
C_i_bcast = C_i.broadcast((nl.tile_size.pmax, seq_len))
scanC_i = nisa.tensor_tensor(scan_i, C_i_bcast, op=ml.multiply)
```


### Step 6: Accumulation of scanC_i along `state_size` dimension

So far in Step 1-5, all the computation is logically parallel across the `state_size` dimension in a Mamba layer. The
next step of computation introduces data dependency along the `state_size` dimension for the first time. The PyTorch reference
implementation is:


```python
# scan_res: [batch_size, channels, state_size, seq_len]
# C:        [batch_size, state_size, seq_len]
# -2 dim is state_size
scanC.sum(dim=-2)

# Holding batch constant only.
# scan_i_states: [channels_tiled, state_size, seq_len]
(scanC_i).sum(dim=-2)
```


In NKI, we can accumulate the `scanC_i` results across states element-wise using `state_size-1` number of `nisa.tensor_tensor`
instructions:


> **Figure: mamba step6**
>
> A tensor summation diagram for Mamba Step 6, showing the reduction of multiple scanC_i tensors (indexed 0 through n-1) to produce a single scanC_i_sum output tensor.
>
> This diagram illustrates Step 6 of the Mamba kernel implementation, showing the final summation/reduction operation across the state dimension.
>
> Four tensors are displayed from left to right, connected by addition and equals operators:
>
> The first three tensors (purple/lavender colored) represent individual scanC_i results for different state indices:
> - "scanC_i[0]" - first state component with dimensions seqlen (F dim) by channels_tiled (P dim)
> - "scanC_i[1]" - second state component with same dimensions
> - "scanC_i[n-1]" - final state component (with ellipsis "..." between [1] and [n-1] indicating intermediate components)
>
> Each purple tensor has the same dimensions:
> - Width: seqlen (F dim) - sequence length on free dimension
> - Height: channels_tiled (P dim) - tiled channels on partition dimension
>
> Plus signs (+) appear between the tensors, with "..." indicating additional summands.
>
> On the right, the result tensor "scanC_i_sum" is shown in light blue, with the same dimensions seqlen (F dim) by channels_tiled (P dim).
>
> An equals sign (=) separates the summation from the result.
>
> This step performs a reduction across the state dimension (n states), summing all the scanC_i components to produce the final output contribution.
>
> **Key Elements:**
> - **scanC_i[0]**: First purple tensor in summation
> - **scanC_i[1]**: Second purple tensor in summation
> - **scanC_i[n-1]**: Last purple tensor in summation (n-th state)
> - **scanC_i_sum**: Light blue output tensor containing the sum
> - **seqlen (F dim)**: Sequence length on free dimension for all tensors
> - **channels_tiled (P dim)**: Tiled channels on partition dimension for all tensors
> - **Plus signs (+)**: Addition operators showing element-wise summation
> - **Ellipsis (...)**: Indicates additional intermediate tensors in the summation


Fig. 35 Step 6: `state_size-1` number of `nisa.tensor_tensor`

Since we will be looping over different states, we can also declare an empty accumulation buffer `scanC_accum` of shape
`[channels_tiled, seq_len]` outside of the loop structure and accumulate into this buffer at the end of the every loop
iteration using `+=` operator. The use of a single accumulation buffer avoids allocating memory for `scanC_i` across
all states in SBUF. The corresponding NKI code is:


```python
scanC_accum = nl.zeros(...)

for i_state in range(state_size):
    scanC_i = ...
    scanC_accum += scanC_i
```


## Initial NKI Kernel

Putting all the pieces together from the previous section, we can arrive at the below kernel implementation `mamba_v1`:


```python
import nki
import nki.language as nl
import nki.isa as nisa
import numpy as np

@nki.jit
def mamba_v1(delta, u, A, B, C):
    """Computes the SSM operation in the Mamba model.

    :param delta: (batch_size, channels, seq_len)
    :param u: (batch_size, channels, seq_len)
    :param A: (channels, state_size)
    :param B: (batch_size, state_size, seq_len)
    :param C: (batch_size, state_size, seq_len)
    :return: (batch_size, channels, seq_len)
    """
    batch_size, channels, seq_len = delta.shape
    output = nl.ndarray((batch_size, channels, seq_len), dtype=delta.dtype,
                        buffer=nl.shared_hbm)

    _, state_size = A.shape

    # We can relax this using mask paramters in all the NKI API calls
    assert channels % 128 == 0

    # Map channels to the partition dimension
    # Tile channels to comply with NKI tile size constraints
    channel_psize = nl.tile_size.pmax
    n_channel_tile = channels // channel_psize

    # Most outer loop with batch_size, parallel_for
    for i_batch in range(batch_size):
        # partial accumulated scanC result with processed states
        scanC_accum = nl.zeros((n_channel_tile, nl.par_dim(channel_psize), seq_len), dtype=delta.dtype)

        # Second outer loop with state_size, partial parallel
        for i_state in range(state_size):

            # Inner loop: tiling channels
            for i_channel_tile in range(n_channel_tile):
                channel_start = i_channel_tile * channel_psize

                # Load the relevant tile from delta and A
                delta_i = nl.load(delta[i_batch, channel_start:channel_start+channel_psize, 0:seq_len])
                A_i = nl.load(A[channel_start:channel_start+channel_psize, i_state])

                # Step 1&2: Element-wise multiplication of delta_i and A_i and then exponential
                deltaA = nisa.activation(op=nl.exp, data=delta_i, scale=A_i)

                # Load the relevant tile from u and B
                u_i = nl.load(u[i_batch, channel_start:channel_start+channel_psize, 0:seq_len])
                B_i = nl.load(B[i_batch, i_state:i_state+1, 0:seq_len])

                # Step 3: Element-wise multiplication of delta_i, B_i and u_i
                deltaU = nisa.tensor_tensor(delta_i, u_i, op=nl.multiply)
                B_i_bcast = B_i.broadcast_to((channel_psize, seq_len))
                deltaBu = nisa.tensor_tensor(deltaU, B_i_bcast, op=nl.multiply)

                # Step 4: Associative scan between deltaA and deltaBu
                scan_res = nki.isa.tensor_tensor_scan(deltaA, deltaBu, initial=0,
                        op0=np.multiply, op1=np.add)

                # Load the relevant tile from C
                C_i = nl.load(C[i_batch, i_state:i_state+1, 0:seq_len])

                # Step 5: Element-wise multiplication of scan_res and C_i
                C_i_bcast = C_i.broadcast_to((channel_psize, seq_len))
                scanC = nisa.tensor_tensor(scan_res, C_i_bcast, op=nl.multiply)

                # Step 6: Accumulation of scanC along state_size dimension
                # scanC_accum[i_channel_tile, 0:channel_psize, 0:seq_len] = nisa.tensor_tensor(
                #         scanC_accum[i_channel_tile, 0:channel_psize, 0:seq_len], scanC, op=nl.add)
                scanC_accum[i_channel_tile, 0:channel_psize, 0:seq_len] += scanC

        # Store scanC_accum for a single batch to output
        for i_channel_tile in range(n_channel_tile):
            channel_start = i_channel_tile * channel_psize
            nl.store(output[i_batch, channel_start:channel_start+channel_psize, 0:seq_len],
                    scanC_accum[i_channel_tile, 0:channel_psize, 0:seq_len])

    return output
```


In the above code example,

* 
We have three levels of loop nests. From the outer-most to inner-most:

Iterating over `batch`: Different batch samples perform completely different computation. `A` tensor is the only
input parameter that is shared among batch samples.

* Iterating over `state_size`: Different states perform parallel computation until Step 6 as discussed in the previous
section. Both `delta` and `u` tensors are shared across different states.

* Iterating over `channels`: This is the most-inner dimension where we tile the input channels dimension into `nl.tile_size.pmax=128`
chunks. Both `B` and `C` tensors are shared across different `channels`.

* The kernel above assumes channels is a multiple of `nl.tile_size.pmax=128` . We can relax this by adding a `mask`
parameter in all the NKI API call in the kernel. To simplify the code example, we omit this change.
See NKI API Masking for more information.

* We declare an empty intermediate tensor `scanC_accum` to hold partial summation from every state.

* 
Within the inner loop, we process data for `nl.tile_size.pmax=128` channels for one batch sample in one state.

We use the slicing syntax
to index a tensor. For example, `delta[i_batch, channel_start:channel_start+channel_psize, 0:seq_len]` grabs data from
the input `delta` tensor for the current range of channels at the current batch sample.

* Note, in tensor slicing, the first index dimension from the left with a slicing range will be chosen as the partition
dimension. When loading `B`, since we intend to load only one state’s worth of data into one partition of SBUF (discussed
in Step 3), we need to explicitly slice the state using: `nl.load(B[i_batch, **i_state:i_state+1**, 0:seq_len])`. Otherwise,
`nl.load(B[i_batch, **i_state**, 0:seq_len])` will treat `seq_len` as the partition dimension, which is not what we
planned for in Step 3 and would also trigger a NKI compilation error since `seq_len` exceeds `nl.tile_size.pmax`.

* We accumulate partial `scanC_i` results into the accumulation buffer using the `+=` operator. This creates a loop-carried
dependency for `scanC_accum` on the `i_state` loop.

### Performance Check

Let’s re-run neuron-profile on the above NKI kernel:


> **Figure: mamba v1 profile**
>
> A Neuron profiler timeline showing the Mamba v1 optimized implementation with significantly improved execution time of 172.93 microseconds, demonstrating better compute utilization compared to the torch reference.
>
> This profiler visualization displays the complete execution timeline spanning from 0 to approximately 180,000 microseconds on the horizontal axis. The view is organized into multiple horizontal tracks showing different hardware components and metrics.
>
> The top section contains queue and spill/reload tracks. qGpSimdIO0 shows red activity marks near the end of execution around 160,000 us. qScalarSpillReload0 displays a cluster of multi-colored activity (cyan, orange, pink) around the 15,000-25,000 us range. qSyncIO0 shows blue and orange marks in the early portion of execution. qSyncSpillReload0 displays green marks at the beginning.
>
> The compute engine tracks show improved utilization. SyncE displays blue horizontal bars with activity concentrated in the first third of execution. TensorE shows green marks scattered from 10,000-70,000 us, followed by a long continuous blue bar at the far right. TensorMatrixE displays red marks clustered between 20,000-70,000 us indicating matrix operations. VectorE shows cyan marks distributed across the timeline with long blue bars at the end. ScalarE presents blue and green marks with an extended blue bar near completion. GpSimdE shows green marks early and blue bars at the end.
>
> The memory section shows State Buffer Usage as a stacked colored area chart that ramps up, maintains high usage through the middle portion, then gradually decreases. PSUM Usage appears as a multi-colored stacked area below it. Sem 0 tracks semaphore activity. Pending DMA Count shows a red line with a spike around 20,000 us. DMA Throughput displays as a green line showing data movement rates. The total execution time of 172.93 us is noted at the bottom.
>
> **Key Elements:**
> - **Execution time**: 172.93 microseconds total (major improvement from 133.43 ms reference)
> - **TensorE/TensorMatrixE**: Active matrix computation marks showing tensor engine utilization
> - **VectorE/ScalarE**: Continuous bars at the end indicating optimized vector and scalar operations
> - **State Buffer Usage**: Colored stacked area showing efficient memory utilization pattern
> - **PSUM Usage**: Multi-colored area chart showing partial sum memory allocation
> - **Pending DMA Count**: Red spike early in execution showing DMA queue activity
> - **DMA Throughput**: Green line tracking data movement bandwidth
> - **Timeline scale**: 0 to 180,000 us with activity concentrated in first 80,000 us


Fig. 36 Profile of initial Mamba kernel implementation `mamba_v1`

Hooray! This NKI kernel implementation now takes `172.93` usec, which is **878x** speedup compared to the reference PyTorch
implementation. Based on the profile, VectorE is the busiest compute engine in the Mamba layer. This makes sense because
the bulk of computation in the kernel is in `nisa.tensor_tensor`, which can only run on VectorE.

Therefore, our goal is to keep VectorE as busy as possible throughout execution. Note, every NEFF execution involves certain
start-up and tear-down overhead. We can use the `Selection Summary` feature in `neuron-profile` to find out the percentage
of time VectorE is busy during the actual execution period:


> **Figure: mamba v1 profile zoomed**
>
> A zoomed-in Neuron profiler view of the Mamba v1 implementation with a Selection Summary panel showing that the Vector Engine achieves 98.71% active duration, indicating excellent compute utilization.
>
> This profiler screenshot shows a detailed timeline view spanning from 0 to approximately 150,000 microseconds, with a total window of 246.2 us displayed at the bottom. The visualization focuses on compute engine activity and memory usage patterns.
>
> The top portion shows compute engine tracks. TensorMatrixE displays red rectangular marks indicating matrix tensor operations, with activity concentrated in the first half of the timeline. VectorE shows a dense pattern of blue horizontal bars interspersed with green marks, demonstrating continuous vector engine activity throughout the execution. ScalarE displays blue bars with scattered black marks. GpSimdE shows blue bars at the beginning and end of the timeline.
>
> The memory section displays State Buffer Usage as a multi-colored stacked area chart (green, blue, pink, yellow, orange layers) that rises, maintains a plateau through the middle portion, and then gradually decreases toward the end. PSUM Usage appears as a similar stacked area below. Sem 0 tracks semaphore state. Pending DMA Count shows a red line with a spike early in execution around the 25,000 us mark. DMA Throughput displays as a green line tracking bandwidth, and Estimated MFU appears at the bottom.
>
> Below the timeline, a toolbar shows various blue buttons including Search, Annotations, Edit view settings, Summary, Layer Summary, Selection Summary, NEFF Header, NEFF Nodes, Model Info, DMA Queues Info, and NC Mem. A Selection Summary popup panel is displayed showing detailed metrics for a selected region.
>
> **Key Elements:**
> - **VectorE track**: Dense blue bars showing 98.71% active utilization (181 events)
> - **TensorMatrixE**: Red marks indicating tensor matrix operations
> - **ScalarE/GpSimdE**: Blue bars showing additional compute engine activity
> - **Selection Summary panel**: Shows Duration: 150.93 us, Start Time: 16.92 us, End Time: 167.84 us, Event Count: 181, Event Duration Sum: 193.94 us, Event Duration Active: 148.99 us (98.71%)
> - **State Buffer Usage**: Colorful stacked area chart showing memory utilization pattern
> - **Pending DMA Count**: Red line with early spike indicating DMA queue depth
> - **DMA Throughput**: Green line tracking data movement bandwidth
> - **Timeline scale**: 0 to 150,000 us within 246.2 us total window


Fig. 37 Profile of initial Mamba kernel implementation `mamba_v1` (zoomed in)

As indicated by the above profile, VectorE is active over **98.71%** of the time, which is rather impressive. However,
remember we used small input shapes as a toy example to get started: `[batch=1, seq_len=512, channels = 256, n = 16]`.
Next, let’s increase the `channels` and `seq_len` dimensions one by one and observe how VectorE efficiency changes.

## Increasing input `channels` size

Let’s increase the size of `channels` by 16x, from 256 to a more realistic value 4096. We obtain the following profile:

[![../../../_images/mamba_v1_profile_4k_chan.png](../../../_images/mamba_v1_profile_4k_chan.png)](../../../_images/mamba_v1_profile_4k_chan.png)

Fig. 38 Profile of `mamba_v1` kernel with 4K channels

The new device execution time with increased channels is now **2.34 ms**. We can see that VectorE active duration has
dropped to **92.16%** during the core execution period, compared to **98.71%** previously with the toy example. Let’s zoom
into an arbitrary region of the profile to see what could be causing VectorE to go idle:

[![../../../_images/mamba_v1_profile_4k_chan_sem.png](../../../_images/mamba_v1_profile_4k_chan_sem.png)](../../../_images/mamba_v1_profile_4k_chan_sem.png)

Fig. 39 `mamba_v1` kernel blocking on input tensor loading

By identifying a gap where VectorE is completely idle, we can hover over the first executed instruction after the gap
to find out what’s the reason for idleness in the instruction semaphore wait condition. In the above screenshot, the instruction
is pending on `S[22]` to reach a value of 240, which is set by `qSyncIO0` activities. This means VectorE has been waiting
for input tensors to be loaded before performing more computation. If you hover over `qSyncIO0` activities during the
VectorE idle period, you can also see the exact input tensor name defined in NKI being loaded in the DMA:

[![../../../_images/mamba_v1_profile_4k_chan_load_var.png](../../../_images/mamba_v1_profile_4k_chan_load_var.png)](../../../_images/mamba_v1_profile_4k_chan_load_var.png)

Fig. 40 DMA loading tensor u in `mamba_v1` profile

We can find similar VectorE gaps through the execution trace. At this point, we can conclude one of the reasons why we have
a lower VectorE active time percentage is due to *blocking* input tensor loading (`nl.load`) activities in the DMA.
Next, let’s spend some time analyzing DMA efficiency.

Zooming out, we can make several observations. First, we see two orange boxes around the `qSyncIO0` row. Hovering over
the top left corners of the boxes shows two similar performance warnings for loading IO tensors:

[![../../../_images/mamba_v1_profile_4k_chan_reload.png](../../../_images/mamba_v1_profile_4k_chan_reload.png)](../../../_images/mamba_v1_profile_4k_chan_reload.png)

Fig. 41 Performance warnings for reloading `u` and `delta` tensors

This indicates we reload both the input `u` and `delta` tensors around 7 times. This could be inevitable
when we don’t have sufficient on-chip memory (SBUF) to allow full reuse of the input data tensors. However, the profiler
shows we are only hitting around 50% capacity usage throughout execution:

[![../../../_images/mamba_v1_profile_4k_chan_sb.png](../../../_images/mamba_v1_profile_4k_chan_sb.png)](../../../_images/mamba_v1_profile_4k_chan_sb.png)

Fig. 42 Low SBUF usage

Therefore, the input tensor reloading is likely not justified, and we should investigate whether we can optimize the
NKI kernel to avoid it.

### Minimizing data reloading by loop reordering

To understand why delta and u are being reloaded, let’s revisit our input tensor shapes:

* `delta: [batch_size, channels, seq_len]`

* `u:&#160;&#160;&#160;&#160; [batch_size, channels, seq_len]`

* `A:&#160;&#160;&#160;&#160; [channels, state_size]`

* `B:&#160;&#160;&#160;&#160; [batch_size, state_size, seq_len]`

* `C:&#160;&#160;&#160;&#160; [batch_size, state_size, seq_len]`

Let’s hold `batch_size` constant since the majority of input tensors have completely different slices for different batch
samples:

* `delta: [channels, seq_len]`

* `u:&#160;&#160;&#160;&#160; [channels, seq_len]`

* `A:&#160;&#160;&#160;&#160; [channels, state_size]`

* `B:&#160;&#160;&#160;&#160; [state_size, seq_len]`

* `C:&#160;&#160;&#160;&#160; [state_size, seq_len]`

`delta` and `u` tensors have the same shape with `channels` as the outer dimensions, while `B` and `C` have the
same shape with `state_size` as the outer dimension. All four of these input tensors have `seq_len` as the inner dimension.
Therefore, we say `delta/u` is reused across different states, while `B/C` are reused across different channels. Given
this conflicting reuse dimensions, we further say it is more important to **prioritize reuse of ``delta/u``** because
the expected size of `channels` is much higher than `state_size`:

* `state_size` is now 16 and typically stay small

* `channels` is now 4096 and typically in the thousands

In NKI, we can prioritize `delta/u` reuse through loop ordering. Recall in the initial NKI kernel implementation, we have
the following inner loops:


```python
...
for i_state in range(state_size):
    for i_channel_tile in range(n_channel_tile):
        # step 1-6
...
```


Since these two loops are executed serially within a single NeuronCore, the loop instances will be unrolled by Neuron Compiler.
With the channel dimension in the fastest dimension, we will need to load `delta/u` across all channels in the first state,
and then likely reload them again in the later states due to a large total memory size in `delta` and `u` (16MB in this
case).

To prioritize reuse of `delta/u`, we should reorder the above loop nests. To further enforce the reuse, we can hoist
the `nl.load` calls for `delta/u` outside of the `i_state` inner loop:


```python
...
for i_channel_tile in range(n_channel_tile):
    delta_i = nl.load(...)
    u_i = nl.load(...)

    for i_state in range(state_size):
        # step 1-6
...
```


As a side effect of this loop re-ordering, we can also spot a loop fusion opportunity since we have two `i_channel_tile`
loop nests at the same level now:


```python
scanC_accum = nl.zeros((n_channel_tile, nl.par_dim(channel_psize), seq_len), ...)
...

# First i_channel_tile loop
for i_channel_tile in range(n_channel_tile):
    delta_i = nl.load(...)
    u_i = nl.load(...)

    for i_state in range(state_size):
        # step 1-6

# Second i_channel_tile loop
for i_channel_tile in range(n_channel_tile):
    nl.store(..., scanC_accum[i_channel_tile, 0:channel_psize, 0:seq_len])

...
```


By fusing the two `i_channel_tile` loop nests into a single loop nest, we can pull the declaration of `scanC_accum`
inside the `i_channel_tile` loop and further reduce the `scanC_accum` size requirement by a factor of `n_channel_tile`
:


```python
...

# First i_channel_tile loop
for i_channel_tile in range(n_channel_tile):
    scanC_accum = nl.zeros((nl.par_dim(channel_psize), seq_len), ...)

    delta_i = nl.load(...)
    u_i = nl.load(...)

    for i_state in range(state_size):
        # step 1-6

    nl.store(..., scanC_accum[i_channel_tile, 0:channel_psize, 0:seq_len])

...
```


Let’s modify our initial NKI kernel implementation accordingly to get `mamba_v2`:


```python
@nki.jit
def mamba_v2(delta, u, A, B, C):
    """Computes the SSM operation in the Mamba model.

    :param delta: (batch_size, channels, seq_len)
    :param u: (batch_size, channels, seq_len)
    :param A: (channels, state_size)
    :param B: (batch_size, state_size, seq_len)
    :param C: (batch_size, state_size, seq_len)
    :return: (batch_size, channels, seq_len)
    """
    batch_size, channels, seq_len = delta.shape
    output = nl.ndarray((batch_size, channels, seq_len), dtype=delta.dtype,
                        buffer=nl.shared_hbm)
    _, state_size = A.shape

    assert channels % 128 == 0

    # Map channels to the partition dimension
    # Tile channels to comply with NKI tile size constraints
    channel_psize = nl.tile_size.pmax
    n_channel_tile = channels // channel_psize

    # Most outer loop with batch_size, parallel_for
    for i_batch in range(batch_size):

        # Second outer loop: tiling channels
        for i_channel_tile in range(n_channel_tile):
            channel_start = i_channel_tile * channel_psize

            # partial accumulated scanC result with processed states
            scanC_accum = nl.zeros((nl.par_dim(channel_psize), seq_len), dtype=delta.dtype)

            # Load delta/u once to be reused across states
            delta_i = nl.load(delta[i_batch, channel_start:channel_start+channel_psize, 0:seq_len])
            u_i = nl.load(u[i_batch, channel_start:channel_start+channel_psize, 0:seq_len])

            # Inner loop with state_size, partial parallel
            for i_state in range(state_size):
                # Load the relevant tile from A
                A_i = nl.load(A[channel_start:channel_start+channel_psize, i_state])

                # Step 1&2: Element-wise multiplication of delta_i and A_i and then exponential
                deltaA = nisa.activation(op=nl.exp, data=delta_i, scale=A_i)

                # Load the relevant tile from B
                B_i = nl.load(B[i_batch, i_state:i_state+1, 0:seq_len])

                # Step 3: Element-wise multiplication of delta_i, B_i and u_i
                deltaU = nisa.tensor_tensor(delta_i, u_i, op=nl.multiply)
                B_i_bcast = B_i.broadcast_to((channel_psize, seq_len))
                deltaBu = nisa.tensor_tensor(deltaU, B_i_bcast, op=nl.multiply)

                # Step 4: Associative scan between deltaA and deltaBu
                scan_res = nki.isa.tensor_tensor_scan(deltaA, deltaBu, initial=0,
                        op0=np.multiply, op1=np.add)

                # Load the relevant tile from C
                C_i = nl.load(C[i_batch, i_state:i_state+1, 0:seq_len])

                # Step 5: Element-wise multiplication of scan_res and C_i
                C_i_bcast = C_i.broadcast_to((channel_psize, seq_len))
                scanC = nisa.tensor_tensor(scan_res, C_i_bcast, op=nl.multiply)

                # Step 6: Accumulation of scanC along state_size dimension
                scanC_accum[0:channel_psize, 0:seq_len] += scanC

            # Store scanC_accum for a single batch to output
            nl.store(output[i_batch, channel_start:channel_start+channel_psize, 0:seq_len],
                    scanC_accum[0:channel_psize, 0:seq_len])

    return output
```


We recapture the profile for the new kernel implementation:


> **Figure: mamba v2**
>
> A Neuron profiler timeline showing the Mamba v2 optimized implementation with a Selection Summary panel displaying Vector Engine achieving approximately 99.67% active duration, demonstrating further improved compute utilization.
>
> This profiler visualization displays the execution timeline of the Mamba v2 kernel optimization. The horizontal timeline spans from 0 to approximately 1,500,000 microseconds based on the scale markers visible (200,000, 400,000, 600,000, 800,000, 1,000,000, 1,200,000, 1,400,000).
>
> The top section shows multiple activity tracks for different hardware queues and engines. Several tracks show sparse activity marks in various colors including red, blue, and green, representing different types of operations. The TensorMatrixE track shows periodic red marks indicating matrix operations. The VectorE track displays dense activity with green marks and blue bars throughout the execution period.
>
> The middle section contains memory usage visualizations. State Buffer Usage appears as a multi-colored stacked area chart showing memory buffer utilization over time. The colors include green, blue, pink, yellow, and orange representing different memory allocations. PSUM Usage shows a similar stacked area pattern below it.
>
> The lower portion displays DMA-related metrics. Pending DMA Count appears as a red line showing queue depth. DMA Throughput displays as a green line tracking data movement bandwidth, with visible spikes at regular intervals corresponding to data transfer operations.
>
> At the bottom, a toolbar with blue buttons is visible, including Search, Annotations, Edit view settings, Summary, Layer Summary, Selection Summary, NEFF Header, NEFF Nodes, Model Info, DMA Queues Info, NC Memory Usage Info, and more. A Selection Summary popup shows detailed metrics for VectorE activity with count of 2086 events and active duration of approximately 99.67%.
>
> **Key Elements:**
> - **VectorE track**: Shows 2086 events with ~99.67% active duration (improvement over v1)
> - **TensorMatrixE**: Periodic red marks indicating matrix tensor operations
> - **State Buffer Usage**: Multi-colored stacked area showing memory utilization
> - **PSUM Usage**: Stacked area chart below state buffer showing partial sum allocation
> - **DMA Throughput**: Green line showing periodic data transfer activity
> - **Pending DMA Count**: Red line tracking DMA queue depth
> - **Selection Summary panel**: Displays Duration, Start/End Time, Event Count, Event Duration Sum, and Event Duration Active metrics
> - **Timeline scale**: Spans 0 to ~1,500,000 us showing full kernel execution


Fig. 43 Profile of `mamba_v2` kernel with loop reordering optimization

The device execution time is now **1.61 ms**, which is a **31%** reduction in latency compared to our initial kernel implementation.
We can also see VectorE active duration is back up to 99.63% and the performance warnings on input tensor reloading are
now gone. In case you are curious, the above loop reordering optimization alone provides around 30% of latency reduction,
while the loop fusion optimization contributes the remaining 1% performance boost. This makes sense because the loop reordering
addresses our key performance concern around input data reloading, while reducing intermediate tensor size is only a nice-to-have
given we were quite low on SBUF usage to begin with.

## Increasing input `seq_len` size

Next, let’s increase the input `seq_len` by **16x**, from 512 to 8192 and recompile the above NKI kernel. Below is the
associated performance profile:

[![../../../_images/mamba_v2_8K_seqlen.png](../../../_images/mamba_v2_8K_seqlen.png)](../../../_images/mamba_v2_8K_seqlen.png)

Fig. 44 Profile of `mamba_v2` kernel with 8K seq_len

The new profile now takes **53.33 ms**, which is **33x longer** than the previous profile. VectorE active duration has
dropped down to a new low: 58.93%. Compared to the profile captured with a smaller `seq_len`, we notice new DMA activity
rows `qSyncSpillReload0` and `qVectorSpillReload0` , which are associated with data movement traffic for intermediate
data spill from SBUF into device memory or reload back to SBUF. Zooming into a smaller portion of the profile:

[![../../../_images/mamba_v2_8K_seqlen_zoomed.png](../../../_images/mamba_v2_8K_seqlen_zoomed.png)](../../../_images/mamba_v2_8K_seqlen_zoomed.png)

Fig. 45 Poor overlap of computation and data movement

We can see VectorE enters idle states due to a blocking semaphore wait for `qSyncSpillReload0` activities,
which indicates the extra spill/reload is indeed degrading overall computation performance. In addition, we can see low
SBUF usage peaking at merely 50%. Computation and data movement are also not overlapped properly, leading to low average
utilization in both compute engines and DMA throughput in the overall timeline.

Intuitively, increasing `seq_len` of the kernel increases the active tile sizes of input and intermediate tensors in the
free dimension, which could cause severe fragmentation in SBUF and excessive data movements to spill/reload tensors in
SBUF. To mitigate these inefficiencies, we must **tile** the `seq_len` dimension in our NKI kernel through a new loop
level.

### Mitigate spilling by tiling `seq_len`

We have **three** key considerations when adding this new loop level:

* tile size selection,

* loop-carried dependency handling

* loop ordering with other loop nests.

**Tile size of ``seq_len``.** Since previously with `seq_len=512` in our toy example, we were able to achieve close to
100% VectorE utilization, let’s set the tile size `seq_len_fsize` to 512 as a starting point. We can revisit this decision
as needed once we obtain a new profile.

**Loop-carried dependency.** Splitting `seq_len` into chunks is straightforward for all computation steps except for Step
4. In the associative scan operation, the next loop iteration requires results from the previous iteration for computation.
As a result, we will introduce another loop-carried dependency here with the scan tiles. This dependency can be handled
through the `initial` input parameter:


```python
scan_init = nl.zeros((channel_psize, 1), ...)

for i_seq_len_tile in range(seq_len // seq_len_fsize):
    scan_i = nisa.tensor_tensor_scan(deltaA, deltaBu, initial=scan_init,
                                          op0=np.multiply, op1=np.add)
    scan_init = scan_i[0:channel_psize, seq_len_fsize-1]
```


Note the loop-carried dependency: `scan_init` is updated each iteration and used as the initial value in the next.

**Loop ordering.** Recall from our latest NKI kernel implementation, we have the following loop nest:


```python
...
for i_batch in range(batch_size):

    for i_channel_tile in range(n_channel_tile):
        scanC_accum = nl.zeros((nl.par_dim(channel_psize), **seq_len**), ...)

        delta_i = nl.load(delta[i_batch, channel_start:channel_start+channel_psize, 0:**seq_len**])
        u_i = nl.load(u[i_batch, channel_start:channel_start+channel_psize, 0:**seq_len**])

        for i_state in range(state_size):
            A_i = nl.load(A[channel_start:channel_start+channel_psize, i_state])

            B_i = nl.load(B[i_batch, i_state:i_state+1, 0:**seq_len**])
            C_i = nl.load(C[i_batch, i_state:i_state+1, 0:**seq_len**])

            deltaA = ...
            deltaBu = ...
            scanC = ...
            ...
            scanC_accum += ...

         nl.store(..., scanC_accum[i_channel_tile, 0:channel_psize, 0:**seq_len**])
...
```


Let’s denote the above loop ordering as `[batch_size, n_channel_tile, state_size]`, and our key question here is where
to insert `seq_len` in this list.

Appending `seq_len` to the above list, that is, making `seq_len` the new inner-most loop, would involve the least amount
of code changes to our current NKI kernel. However, it will lead to the least amount of SBUF usage reduction, since this
loop ordering won’t be tiling `scanC_accum`, `delta_i` and `u_i` tensors. Given `seq_len=8192` and FP32 data types,
these three tensors will occupy 8192*4B*3 = 96 KiB/partition, half of the available SBUF capacity. Let’s go ahead and
experiment this loop ordering in a new kernel `mamba_v3`:


> **Figure: mamba v3**
>
> A Neuron profiler timeline showing the Mamba v3 implementation with an extended execution timeline, displaying Vector Engine utilization at 94.80% active duration across 38,009 events.
>
> This profiler visualization displays the execution timeline of the Mamba v3 kernel optimization. The horizontal timeline spans from 0 to approximately 25,000,000 microseconds, showing a longer execution period compared to previous versions, with markers at 5,000,000, 10,000,000, 15,000,000, 20,000,000, and 25,000,000.
>
> At the top, queue activity tracks are displayed. A prominent orange/yellow continuous bar spans the entire timeline in the qSyncIO0 track, indicating constant I/O synchronization activity throughout execution. Below this, multiple tracks show various queue and engine activities with colored marks and bars.
>
> The compute engine section shows SyncE with blue marks and bars indicating synchronization operations. TensorE displays sparse green and blue marks. TensorMatrixE shows red marks for matrix operations distributed across the timeline. VectorE contains cyan and blue marks showing vector engine activity. ScalarE displays scattered activity marks, and GpSimdE shows periodic activity.
>
> The memory section displays State Buffer Usage as a relatively flat multi-colored stacked area, indicating consistent memory utilization throughout execution. PSUM Usage appears as a thin layer below it. DMA-related tracks show Pending DMA Count as occasional spikes and DMA Throughput as a green line near the bottom.
>
> The bottom portion shows a toolbar with blue buttons including Search, Annotations, View Settings, Summary, Layer Summary, Selection Summary, NEFF Header, NEFF Nodes, Model Info, DMA Queues Info, NC Memory Usage Info, Summarize, and Help. A Selection Summary popup displays detailed metrics showing Duration, Start Time, End Time, Event Count of 38,009, Event Duration Sum, and Event Duration Active at 94.80%.
>
> **Key Elements:**
> - **qSyncIO0**: Continuous orange/yellow bar indicating persistent I/O sync activity
> - **VectorE activity**: 38,009 events with 94.80% active duration
> - **TensorMatrixE**: Red marks distributed across the timeline for matrix operations
> - **State Buffer Usage**: Flat multi-colored stacked area showing consistent memory usage
> - **Timeline scale**: Extended scale from 0 to ~25,000,000 us
> - **Selection Summary panel**: Shows VectorE count = 38009, active dur = 94.80%
> - **DMA Throughput**: Green line showing periodic data transfer activity
> - **Execution pattern**: More distributed activity compared to v1/v2 optimizations


Fig. 46 Profile of `mamba_v3` kernel with seq_len tiling optimization

With the above profile, the kernel now takes **27.8 ms**, which is **48%** reduction in latency compared to no `seq_len`
tiling. VectorE is now 94.85% active, and we no longer have spilling related DMA activities.

Finally, since the key advantage of Mamba compared to Transformer models is Mamba’s computation and latency should scale
linearly with respect to `seq_len`, instead of quadratically in Transformers, let’s plot the measured kernel latencies across different
`seq_len` up to 8K (what we have optimized so far) and compare it against “perfect latencies” assuming linear scaling
from `seq_len=512`. We evaluate scaling efficiency using `perfect latency / measured latency`,
which is a higher the better metric. Finally, to showcase the importance of the last seq_len tiling optimization for scaling seq_len,
we also compare scaling efficiency for `mamba_v2` (no seq_len tiling) and `mamba_v3` (seq_len tiling).


| seq_len | Perfect Latency (ms) | mamba_v2 Measured Latency (ms) | mamba_v2 Scaling Efficiency | mamba_v3 Measured Latency (ms) | mamba_v3 Scaling Efficiency |
| --- | --- | --- | --- | --- | --- |
| 512 | N/A | 1.6 | N/A | 1.6 | N/A |
| 1024 | 3.2 | 4.4 | 72.73% | 3.3 | 96.97% |
| 2048 | 6.4 | 8.9 | 71.91% | 6.6 | 96.97% |
| 3072 | 9.6 | 13.1 | 73.28% | 10.1 | 95.05% |
| 4096 | 12.8 | 17.6 | 72.73% | 13.3 | 96.24% |
| 5120 | 16 | 23.7 | 67.51% | 17.3 | 92.49% |
| 6144 | 19.2 | 27.5 | 69.82% | 19.6 | 97.96% |
| 7168 | 22.4 | 41.3 | 54.24% | 24.2 | 92.56% |
| 8192 | 25.6 | 52.2 | 49.04% | 27.8 | 92.09% |

The above data shows the last NKI kernel implementation `mamba_v3` can reach 90%+ scaling efficiency up to 8K `seq_len`.
To support even larger `seq_len`, we will need more aggressive tiling by pulling the `seq_len` loop level further
towards the outer-loop level to tile more input/intermediate tensors to keep spilling low and VectorE busy.

## Download All Source Code

Click the links to download source code of the kernels and the testing code
discussed in this tutorial.

* PyTorch reference implementation: [`mamba_torch.py`](../../downloads/mamba_torch.py)

* Three versions of NKI kernels: [`mamba_nki_kernels.py`](../../downloads/mamba_nki_kernels.py)

You can also view the source code in the GitHub repository [nki_samples](https://github.com/aws-neuron/nki-samples/tree/main/src/nki_samples/tutorials/fused_mamba/)

### Example usage of the scripts:

**Performance mode**

Run PyTorch reference implementation to generate a NEFF for profiling:


```python
python3 mamba_torch.py --mode perf
```


Check performance numbers of mamba_v1/mamba_v2/mamba_v3:


```python
python3 mamba_nki_kernels.py --mode perf --version v1 v2 v3 --batch 1 --seq_len 2048 --channels 512 --state_size 16
```


**Accuracy mode**

Check mamba_v1 NKI kernel accuracy against PyTorch implementation:


```python
python3 mamba_torch.py --mode accuracy
```


Check optimized Mamba kernel (mamba_v2, mamba_v3) accuracy against mamba_v1:


```python
python3 mamba_nki_kernels.py --mode accuracy --version v1 v2 v3 --batch 1 --seq_len 2048 --channels 512 --state_size 16
```