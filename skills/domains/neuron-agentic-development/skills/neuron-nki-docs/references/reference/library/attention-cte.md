# Attention CTE Kernel API Reference

Attention CTE Kernel API Reference
This topic provides the API reference for the `Attention CTE` kernel. The kernel implements attention specifically optimized for Context Encoding (Prefill) use cases with long sequence lengths.

The kernel supports:

* Efficient attention computation for long sequence lengths

* Causal masking

* Sliding window attention

* Context parallelism for distributed computation

* Prefix caching for efficient inference

* Sink tokens for streaming attention

* Native Grouped Query Attention (GQA) support

* Softmax caching for training

## Background

The `Attention CTE` kernel is designed specifically for context encoding (prefill) scenarios where the sequence length is large (typically > 256). It performs the standard attention operation `Attention(Q, K, V) = softmax(scale * Q &#64; K^T) &#64; V` with optimizations for long sequence lengths.

The kernel employs efficient tiling strategies and memory access patterns to maximize performance on Neuron hardware. It supports various optimizations including flash attention for long sequences, LNC sharding, and context parallelism.

## API Reference

**Source code for this kernel API can be found at**: [aws-neuron/nki-library](https://github.com/aws-neuron/nki-library)

### attention_cte

nkilib.core.attention_cte.attention_cte(*q*, *k*, *v*, *scale=1.0*, *causal_mask=True*, *k_prior=None*, *v_prior=None*, *prior_used_len=None*, *sink=None*, *sliding_window=None*, *tp_q=True*, *tp_k=False*, *tp_out=False*, *cache_softmax=False*, *softmax_dtype=nl.float32*, *cp_offset=None*, *global_cp_deg=None*)
Entrypoint NKI kernel that supports multiple attention variants.

The kernel can be invoked with 1D SPMD grid for LNC2 or without grid.

Parameters:

* **q** (`nl.ndarray`) – Query tensor with layout dependent on `tp_q` parameter

* **k** (`nl.ndarray`) – Key tensor with layout dependent on `tp_k` parameter

* **v** (`nl.ndarray`) – Value tensor with shape `(batch_size_kv, seqlen, d)`

* **scale** (`float`, optional) – Scaling factor for attention scores. Must be 1.0 when using sliding window, context parallel, or prefix caching.

* **causal_mask** (`bool`, optional) – Whether to use causal mask

* **k_prior** (`nl.ndarray`, optional) – (Prefix caching) Prior key tensor with layout dependent on `tp_k` parameter

* **v_prior** (`nl.ndarray`, optional) – (Prefix caching) Prior value tensor with shape `(batch_size_kv, seqlen_prior, d)`

* **prior_used_len** (`nl.ndarray`, optional) – (Prefix caching) Actual used length in prior with shape `(1,)`

* **sink** (`nl.ndarray`, optional) – Sink token tensor

* **sliding_window** (`int`, optional) – Sliding window size for attention, `None` or `0` denotes no sliding window mask

* **tp_q** (`bool`, optional) – Query tensor transpose flag

* **tp_k** (`bool`, optional) – Key tensor transpose flag

* **tp_out** (`bool`, optional) – Output tensor transpose flag

* **cache_softmax** (`bool`, optional) – Whether to cache softmax intermediate values

* **softmax_dtype** (`nl.dtype`, optional) – Data type for softmax computations

* **cp_offset** (`nl.ndarray`, optional) – Context parallel offset tensor

* **global_cp_deg** (`int`, optional) – Global context parallel degree

Returns:
Output tensor with attention results. Shape depends on `tp_out` parameter. If `cache_softmax` is `True`, returns tuple of `(output, out_neg_max, out_sum_recip)`.

Return type:
`nl.ndarray` or `tuple`

**IO Shapes**:

* q:
`(batch_size, seqlen_q, d)` when `tp_q` is `True`
`(batch_size, d, seqlen_q)` when `tp_q` is `False`

* k:
`(batch_size_kv, seqlen_kv, d)` when `tp_k` is `True`
`(batch_size_kv, d, seqlen_kv)` when `tp_k` is `False`

* v: `(batch_size_kv, seqlen_kv, d)`

* returns:
`(batch_size, d, seqlen_q)` if `tp_out` is `True`
`(batch_size, seqlen_q, d)` if `tp_out` is `False`

**Constraints**:

* Head dimension (`d`) must be <= 128

* `scale` must be 1.0 when using sliding window, context parallel, or prefix caching

* Context parallelism currently only supports causal attention

* Sliding window attention currently only supports causal attention

## Features

* **Causal Masking (causal_mask=True)**:

Masks upper triangle of attention scores: `S[i,j] = -inf` when `i < j`

* Enables compute skipping: skip MM1/MM2 for upper triangle tiles

* **Sliding Window Attention (SWA, when sliding_window > 0)**:

Local attention: each query only attends to nearby keys within a window

* Masks attention scores: `S[i,j] = -inf` when `|i - j| > sliding_window`

* Currently only works with causal: masks both upper triangle AND positions outside window

* When used with CP: loads only required KV slice to save memory

* **Context Parallelism (CP, global_cp_deg > 1, cp_offset != None)**:

Distributes long sequence computation across multiple devices/ranks

* Each rank (kernel call) processes a slice of Q sequence with full K/V

* `cp_offset` indicates which Q slice this rank handles (runtime value)

* Requires dynamic masking since offset unknown at compile time

* Currently only supports causal attention

* **Prefix Caching (k_prior/v_prior provided)**:

K/V split into two parts: prior (cached) and active (current)

* `prior_used_len` specifies how much of prior to use (dynamic mask)

* Causal mask not required for prior portion (although SWA still applies if enabled)

* **Sink Tokens (sink provided)**:

Add additional sink token to softmax denominator

* **Grouped Query Attention (GQA, batch_size_kv < batch_size)**:

Kernel handles GQA natively without explicit K/V replication

* **Support for training**:

Kernel can optionally return maximum attention score and softmax denominator (per row) for backpropagation

## Implementation Details

The kernel implementation includes several key optimizations:

* **LNC2 Sharding**: Shards computation across 2 NeuronCores with primary sharding on batch dimension and secondary sharding on sequence length for odd batch sizes.

* **Flash Attention**: For K/V length > 10K tokens, divides into 8K-token sections and processes one section at a time to fit in SBUF memory.

* **Software Pipelining**: Overlaps operations across Q groups (`i`, `i+1`, `i+2`) for efficient hardware utilization:

Group `i`: PV computation, writeback

* Group `i+1`: Exp computation

* Group `i+2`: Q load, QK computation

* **Modular Allocation**: Uses efficient buffer reuse with modular allocation for intermediate tensors.

* **Dynamic Masking**: Implements efficient masking strategies for causal, sliding window, and context parallel scenarios.

* **Optimized Memory Access**: Employs careful memory access patterns to optimize data movement between HBM and SBUF.

## Algorithm

The kernel goes through the following steps:

* **Setup**: Initialize intermediate buffers, mask, and debug tensors.

* **Loop over K/V sections**: For long sequences, divide K/V into sections of 8K tokens.

* **For each section**:

Load K and V to SBUF

* Loop over Q (groups) - each group has seqlen 128

* Within each group:

Load Q

* Compute QK^T (MM1) and max

* Compute exponential and transpose

* Compute PV (MM2)

* Write to output

* **Flash Attention**: Maintain running statistics (max, sum) across sections and use these to update the output using flash attention rescaling.

## See Also

* [Attention TKG Kernel API Reference](attention-tkg.md)