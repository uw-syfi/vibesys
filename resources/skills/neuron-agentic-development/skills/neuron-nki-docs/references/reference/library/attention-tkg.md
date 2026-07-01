# Attention TKG Kernel API Reference

Attention TKG Kernel API Reference
This topic provides the API reference for the `Attention TKG` kernel. The kernel implements attention specifically optimized for Token Generation (Decoding) use cases with small active sequence lengths.

The kernel supports:

* Efficient attention computation for small active sequence lengths

* Flexible tensor placement in SBUF or HBM

* Adaptive LNC2 sharding strategies

* In-kernel mask generation

* Fused RoPE (Rotary Position Embedding)

* Block KV cache for efficient long-context inference

* Attention sink for streaming attention

* GPSIMD optimizations for inter-core communication

## Background

The `Attention TKG` kernel is designed specifically for token generation (decoding) scenarios where the active sequence length is small (typically ≤ 7). It performs the standard attention operation `Attention(Q, K, V) = softmax(Q &#64; K^T) &#64; V` with optimizations for small active sequence lengths and large KV caches.

The kernel employs efficient tiling strategies and memory access patterns to maximize performance on Neuron hardware. It supports various optimizations including LNC sharding, block KV cache, and attention sink for streaming attention.

## API Reference

**Source code for this kernel API can be found at**: [aws-neuron/nki-library](https://github.com/aws-neuron/nki-library)

### AttnTKGConfig

*class *nkilib.core.attention_tkg.AttnTKGConfig
Configuration for token-generation attention kernel.

This dataclass contains shape parameters and performance optimization flags
for the attention_tkg kernel, which is optimized for small active sequence lengths.

bs*: [int](https://docs.python.org/3/library/functions.html#int)** = 0*
Batch size

q_head*: [int](https://docs.python.org/3/library/functions.html#int)** = 0*
Number of query heads

s_active*: [int](https://docs.python.org/3/library/functions.html#int)** = 0*
Active sequence length (>1 means speculative decoding)

curr_sprior*: [int](https://docs.python.org/3/library/functions.html#int)** = 0*
Current prior sequence length (KV cache length for this execution)

full_sprior*: [int](https://docs.python.org/3/library/functions.html#int)** = 0*
Full prior sequence length (maximum KV cache capacity)

d_head*: [int](https://docs.python.org/3/library/functions.html#int)** = 0*
Head dimension (embedding size per head)

block_len*: [int](https://docs.python.org/3/library/functions.html#int)** = 0*
Block length for block KV cache (0 if not using block KV)

tp_k_prior*: [bool](https://docs.python.org/3/library/functions.html#bool)** = False*
Specifies that k_prior is transposed (shape `[B, 1, d, s_prior]` instead of `[B, 1, s_prior, d]`)

strided_mm1*: [bool](https://docs.python.org/3/library/functions.html#bool)** = True*
Use strided memory access for first matmul to improve cache locality

use_pos_id*: [bool](https://docs.python.org/3/library/functions.html#bool)** = False*
Generate attention mask from position IDs in-kernel instead of loading pre-generated mask

fuse_rope*: [bool](https://docs.python.org/3/library/functions.html#bool)** = False*
Fuse RoPE (Rotary Position Embedding) computation into the kernel

use_gpsimd_sb2sb*: [bool](https://docs.python.org/3/library/functions.html#bool)** = True*
Use GPSIMD instructions for SBUF-to-SBUF data transfers (LNC2 sharding)

qk_in_sb*: [bool](https://docs.python.org/3/library/functions.html#bool)** = False*
Query and key tensors are already in SBUF instead of HBM

k_out_in_sb*: [bool](https://docs.python.org/3/library/functions.html#bool)** = False*
Output key tensor after RoPE should be stored in SBUF instead of HBM

out_in_sb*: [bool](https://docs.python.org/3/library/functions.html#bool)** = False*
Output tensor should be stored in SBUF instead of HBM

### attention_tkg

nkilib.core.attention_tkg.attention_tkg(*q*, *k_active*, *v_active*, *k_prior*, *v_prior*, *mask*, *out*, *cfg*, *sbm*, *inv_freqs=None*, *rope_pos_ids=None*, *sink=None*, *active_blocks_table=None*, *k_out=None*, *DBG_TENSORS=None*)
Attention specifically optimized for token-gen (where s_active is small). Can optionally fuse RoPE at the start.

Parameters:

* **q** (`nl.ndarray`) – Query tensor. Shape depends on `cfg.qk_in_sb`: If `True`: `[d, B * H * s_active]`, else: `[B, d, H, s_active]`

* **k_active** (`nl.ndarray`) – Active key tensor. Shape depends on `cfg.qk_in_sb`: If `True`: `[d, B * s_active]`, else: `[B, d, s_active]`

* **v_active** (`nl.ndarray`) – Active value tensor. Shape: `[B, 1, s_active, d]`

* **k_prior** (`nl.ndarray`) – Prior key tensor from KV cache. Shape: `[B+, 1, s_prior, d]` if `cfg.tp_k_prior` else `[B+, 1, d, s_prior]`. For block KV cache, shape is `[B+ * block_count, block_len, d]`

* **v_prior** (`nl.ndarray`) – Prior value tensor from KV cache. Shape: `[B+, 1, s_prior, d]`. For block KV cache, shape is `[B+ * block_count, block_len, d]`

* **mask** (`nl.ndarray`) – Attention mask. Shape: `[s_active, B, H, s_active]` if `cfg.use_pos_id` else `[s_prior, B, H, s_active]`

* **out** (`nl.ndarray`) – Output tensor. Shape depends on `cfg.out_in_sb`: If `True`: `[d, B * H * s_active]`, else: `[B, H, d, s_active]`

* **cfg** (`AttnTKGConfig`) – Kernel configuration with shapes and performance flags

* **sbm** (`SbufManager`) – SBUF memory manager for allocating temporary buffers

* **inv_freqs** (`nl.ndarray`, optional) – Inverse frequencies for RoPE. Shape: `[d // 2, 1]`. Required when `cfg.fuse_rope` is `True`

* **rope_pos_ids** (`nl.ndarray`, optional) – Position IDs for RoPE. Shape: `[B, s_active]`. Required when `cfg.fuse_rope` or `cfg.use_pos_id` is `True`

* **sink** (`nl.ndarray`, optional) – Sink attention tokens. Shape: `[H, 1]` for streaming attention sink tokens

* **active_blocks_table** (`nl.ndarray`, optional) – Table of active blocks for block KV cache. Shape: `[B, num_blocks]`. Required when using block KV cache

* **k_out** (`nl.ndarray`, optional) – Output key tensor after RoPE. Shape depends on `cfg.k_out_in_sb`: If `True`: `[d, B * s_active]`, else: `[B, 1, d, s_active]`

* **DBG_TENSORS** (`tuple`, optional) – Optional tuple of 4-5 debug tensors with shared HBM type for intermediate value inspection

Returns:
Tuple of `(out, k_out)` where `out` is the attention output tensor and `k_out` is the key output tensor (if `cfg.fuse_rope` is `True`)

Return type:
`tuple`

**Constraints**:

* Optimized for `s_active <= 7` and `d_head <= 128`

* `cfg.qk_in_sb=True` is required when skipping fused RoPE

* Block KV cache requires `cfg.qk_in_sb=True`

* In-kernel mask generation (`cfg.use_pos_id=True`) is not supported with batch sharding or block KV cache

## Features

* **Flexible Tensor Placement**:

`q`, `k`, `k_out`, and `out` tensors can be placed in either SBUF or HBM

* When `qk_in_sb=True`, q and k tensors are pre-loaded in SBUF (required for block KV cache)

* `out_in_sb` and `k_out_in_sb` flags control output tensor placement for reduced memory transfers

* Use this feature for performance improvement when integrating this kernel into a larger kernel

* **Adaptive LNC2 Sharding**:

Automatically selects sharding strategy based on tensor dimensions

* Batch sharding: Used when batch is even AND (`s_prior < 256` OR `b*q_head*s_active > 128`)

* Sequence sharding: Used when `s_prior >= 256` and batch sharding criteria not met

* Balances computation across 2 NeuronCores for improved throughput

* **Mask Generation**:

`use_pos_id=False`: Pre-generated mask loaded from HBM

* `use_pos_id=True`: Mask generated in-kernel from position IDs

* In-kernel generation reduces memory bandwidth but requires position ID input

* **Fused RoPE (Rotary Position Embedding)**:

`fuse_rope` integrates RoPE computation directly into the attention kernel

* Applies rotary embeddings to Q and K tensors, scaling Q by `1/sqrt(d_head)`

* Reduces memory traffic by avoiding separate RoPE passes

* **Block KV Cache**:

Supports block-sparse KV cache with configurable `block_len`

* Uses `active_blocks_table` to track which cache blocks are active per batch

* Enables efficient long-context inference with sparse memory access patterns

* **K_prior Transpose Handling**:

`tp_k_prior` flag indicates whether K_prior is pre-transposed in memory

* Optimizes memory layout: `[B, 1, d, s_prior]` when `tp_k_prior=True` vs `[B, 1, s_prior, d]` when False

* Reduces transpose operations during computation and improves interoperability with other kernels

* **Strided Memory Access (strided_mm1)**:

Enables strided read patterns for K in first matmul

* When enabled, allows MM2 to use sequential V reads for better DMA throughput

* Trades off MM1 memory access for MM2 optimization

* **Attention Sink**:

* Supports streaming attention with sink tokens for infinite context

* Sink tokens maintain fixed attention scores across all positions

* Integrated into softmax reduction for minimal overhead

* **GPSIMD SBUF-to-SBUF Transfers**:

* `use_gpsimd_sb2sb` enables high-performance GPSIMD instructions for inter-core communication

* Optimizes LNC2 sharding by using extended instructions for SBUF-to-SBUF data transfers

* **Context Length Management**:

`curr_sprior`: Current prior sequence length (actual KV cache content for this invocation)

* `full_sprior`: Full prior sequence length (maximum KV cache capacity allocated)

* Allows progressive filling of KV cache during autoregressive generation

## Implementation Details

The kernel implementation includes several key optimizations:

* **Efficient Tiling Strategy**: Uses carefully chosen tile sizes for processing batches, sequences, and heads to maximize hardware utilization.

* **Cascaded Reduction**: Implements cascaded max and sum reduction operations for softmax computation to maintain numerical stability.

* **Memory Access Optimization**: Employs careful memory access patterns to optimize data movement between HBM and SBUF.

* **Block KV Cache Support**: Implements efficient block-sparse KV cache with dynamic block size adjustment to ensure optimal hardware utilization.

* **Attention Sink Integration**: Efficiently integrates attention sink tokens into the softmax computation for streaming attention.

* **Fused RoPE Implementation**: Implements efficient rotary position embeddings with optimized trigonometric computations.

* **Adaptive Sharding**: Dynamically selects between batch and sequence sharding based on tensor dimensions to optimize performance.

* **GPSIMD Optimization**: Uses GPSIMD instructions for high-performance SBUF-to-SBUF data transfers in LNC2 sharding.

* **Debug Support**: Provides comprehensive debug tensor support for intermediate value inspection.

* **Stack-based SBUF Allocation**: Uses SbufManager for efficient on-chip memory management with hierarchical scoping.

## Algorithm

The kernel goes through the following steps:

* **Setup**: Initialize intermediate buffers, mask, block KV, and debug tensors.

* **Optional RoPE**: If `fuse_rope` is enabled, apply rotary position embeddings to Q and K tensors.

* **KQ^T Computation**: Perform the first matrix multiplication to compute attention scores.

Loop over each batch

* Load the current chunk of K based on configuration (block KV, transpose, etc.)

* Tile over the multiplication of K and Q in groups of 4k size

* **Max Reduction**: Compute the max reduction of KQ^T for softmax stability.

Compute the max in tiles of size 128 over `bs * q_head * s_active`

* Prepare the sink if used

* Transpose and broadcast along the partition dimension

* **Exp(KQ^T - max(KQ^T))**: Apply the exponentiation for softmax computation.

Add/subtract the max based on whether it was negated

* Apply the exponentiation activation

* **Sum Reduction**: Compute sum reduction of the exponentiation result.

Compute the sum in tiles of size 128 over `bs * q_head * s_active`

* Perform additional reductions based on sink or other optimization flags

* Compute the reciprocal with the same tiling scheme, and then broadcast

* **Final Matrix Multiplication**: Compute the product of the softmax output and V and store the result

Loop over each batch

* Load the current chunk of V based on configuration

* Perform the matmul over sprior tiles

* If needed, copy information over core boundaries or to HBM

## See Also

* [Output Projection TKG Kernel API Reference](output-projection-tkg.md)