# MoE Patterns

## Overview
Mixture of Experts (MoE) utility patterns for expert affinity computation, token index loading, block-expert mapping, and expert affinity gathering/broadcasting. These patterns are used across both CTE (Continuous Tensor Engine) and TKG (Token Generation Kernel) MoE implementations.

## Quick Reference

| Function | Module | Signature | Description |
|----------|--------|-----------|-------------|
| `load_block_expert` | CTE | `(block_to_expert, block_idx) -> nl.ndarray` | Load expert ID for current block |
| `load_token_indices` | CTE | `(token_position_to_id, block_idx, B, NUM_TILES) -> nl.ndarray` | Load and transpose token indices (static block) |
| `load_token_indices_dynamic_block` | CTE | `(token_position_to_id, block_idx, B, NUM_TILES, skip_dma) -> nl.ndarray` | Load token indices (dynamic block) |
| `calculate_expert_affinities` | CTE | `(expert_affinities_masked, token_indices, block_expert, E, NUM_TILES, dtype, ...) -> List` | Compute expert affinity scores per token |
| `stream_shuffle_broadcast` | CTE | `(src, dst) -> None` | Broadcast first partition across all partitions |
| `gather_expert_affinities` | TKG | `(expert_affinities_sb, expert_idx, dims, sbm) -> nl.ndarray` | Gather affinities via local_gather |
| `broadcast_token_affinity` | TKG | `(dst, gathered_affinities_sb, token_index, dims, sbm) -> nl.ndarray` | Broadcast per-token affinities across partitions |

## Import Options

**Default** — inline the source into your kernel file.
See the "Full Source Implementation" section below, or the bundled source files in `references/nkilib/core/`.

**If nkilib is installed** in the user's environment:
```python
# CTE MoE utilities
from nkilib.core.moe.moe_cte.moe_cte_utils import (
    load_block_expert,
    load_token_indices,
    load_token_indices_dynamic_block,
    calculate_expert_affinities,
    stream_shuffle_broadcast,
    SkipMode,
)

# TKG MoE utilities
from nkilib.core.moe.moe_tkg.moe_tkg_utils import (
    gather_expert_affinities,
    broadcast_token_affinity,
)
```

## API Documentation

### `load_block_expert(block_to_expert, block_idx) -> nl.ndarray`

Load the expert ID assigned to the current block from the block-to-expert mapping tensor.

**Args:**
- `block_to_expert` (nl.ndarray): Mapping tensor of shape `[N, 1]` where N is number of blocks, containing expert indices
- `block_idx` (int or nl.ndarray): Block index to load, either static integer or dynamic tensor value

**Returns:**
- `nl.ndarray`: Expert ID tensor of shape `[1, 1]` in SBUF (int32)

**Notes:**
- Handles both static (int) and dynamic (tensor) block indices
- Uses `scalar_offset` for dynamic indices via temporary tensor
- Result stored in SBUF for efficient access in subsequent operations

**Example:**
```python
import nki.language as nl

# Static block index
block_expert = load_block_expert(block_to_expert, block_idx=3)

# Dynamic block index (e.g., from loop variable)
block_expert = load_block_expert(block_to_expert, block_idx=dynamic_idx_tensor)
```

---

### `load_token_indices(token_position_to_id, block_idx, B, NUM_TILES) -> nl.ndarray`

Load and transpose token indices for the current block using static block indexing.

**Args:**
- `token_position_to_id` (nl.ndarray): Token position mapping of shape `[N*B]`
- `block_idx` (int): Current block index (static)
- `B` (int): Block size (number of tokens per block)
- `NUM_TILES` (int): Number of tiles (`B // TILE_SIZE`)

**Returns:**
- `nl.ndarray`: Transposed token indices of shape `[TILE_SIZE, NUM_TILES]` in SBUF (int32)

**Notes:**
- Uses `dma_transpose` for efficient layout transformation
- Tokens are distributed across the partition dimension for efficient vector DGE

---

### `load_token_indices_dynamic_block(token_position_to_id, block_idx, B, NUM_TILES, skip_dma) -> nl.ndarray`

Load token indices when block_idx is a dynamic tensor value (runtime-determined).

**Args:**
- `token_position_to_id` (nl.ndarray): Token position mapping tensor
- `block_idx` (nl.ndarray): Dynamic block index tensor
- `B` (int): Block size (number of tokens per block)
- `NUM_TILES` (int): Number of tiles (`B // TILE_SIZE`)
- `skip_dma` (SkipMode): DMA skip configuration

**Returns:**
- `nl.ndarray`: Token indices of shape `[TILE_SIZE, NUM_TILES]` in SBUF

**Notes:**
- Reshapes `token_position_to_id` to `[total_size//B, B]` for indexing
- Uses `scalar_offset` with indirect_dim for dynamic block addressing
- Memsets to zero when `skip_dma.skip_token` is True (for out-of-bounds tokens)

---

### `stream_shuffle_broadcast(src, dst) -> None`

Broadcast the first partition of src onto all partitions of dst.

**Args:**
- `src` (nl.ndarray): 2D input tensor in SBUF
- `dst` (nl.ndarray): 2D output tensor in SBUF (final dim must match src)

**Returns:**
- None: Broadcasts src to dst in-place

**Notes:**
- Uses `nisa.nc_stream_shuffle` with a zero shuffle mask to replicate partition 0
- Processes in banks of 32 partitions

**Example:**
```python
import nki.language as nl

scalar = nl.ndarray((1, 1), dtype=nl.float32, buffer=nl.sbuf)
broadcasted = nl.ndarray((128, 1), dtype=nl.float32, buffer=nl.sbuf)
stream_shuffle_broadcast(src=scalar, dst=broadcasted)
# Now all 128 partitions of broadcasted contain the value from scalar
```

---

### `calculate_expert_affinities(expert_affinities_masked, token_indices, block_expert, E, NUM_TILES, dtype, skip_dma, token_indices_offset) -> List[nl.ndarray]`

Calculate expert affinity scores for tokens in the current block using indirect addressing.

**Args:**
- `expert_affinities_masked` (nl.ndarray): Expert affinities tensor of shape `[(T+1)*E, 1]`
- `token_indices` (nl.ndarray): Token indices of shape `[TILE_SIZE, NUM_TILES]` in SBUF
- `block_expert` (nl.ndarray): Expert ID of shape `[1, 1]` in SBUF
- `E` (int): Number of experts
- `NUM_TILES` (int): Number of tiles for the block
- `dtype`: Data type for affinity values
- `skip_dma` (SkipMode): DMA skip configuration. Default: `SkipMode(False, False)`
- `token_indices_offset` (int): Offset for block tiling. Default: 0

**Returns:**
- `List[nl.ndarray]`: List of expert affinity tensors in SBUF, one per tile, each shape `[TILE_SIZE, 1]` in float32

**Notes:**
- Uses pointer arithmetic: `addr = token_indices * E + block_expert`
- Broadcasts `block_expert` to all partitions via `stream_shuffle_broadcast`
- Performs indirect load from `expert_affinities_masked` using `vector_offset`
- Handles out-of-bounds tokens via `oob_mode.skip` when `skip_dma.skip_token` is True

---

### `gather_expert_affinities(expert_affinities_sb, expert_idx, dims, sbm) -> nl.ndarray`

Gather expert affinities based on expert indices using `local_gather` operation (TKG path).

**Args:**
- `expert_affinities_sb` (nl.ndarray): `[_pmax, E]` expert affinities in SBUF
- `expert_idx` (nl.ndarray): `[T, K]` expert indices for each token
- `dims` (MLPTKGConstantsDimensionSizes): Dimension sizes object
- `sbm` (SbufManager): SBUF memory manager

**Returns:**
- `nl.ndarray`: `[_pmax, 16, 16]` gathered affinities tensor

**Constraints:**
- `K <= 16` (PARTITIONS_PER_CORE)
- `E > 1` (local_gather requires src_buffer_size > 1)

**Notes:**
- Uses different strategies for `T <= 16` (nc_transpose path) vs `T > 16` (dma_transpose path)
- Converts expert indices to uint16 for `local_gather`

---

### `broadcast_token_affinity(dst, gathered_affinities_sb, token_index, dims, sbm) -> nl.ndarray`

Broadcast expert affinities for a specific token across all partitions (TKG path).

**Args:**
- `dst` (nl.ndarray): Destination tensor for broadcasted affinities
- `gathered_affinities_sb` (nl.ndarray): `[_pmax, 16, 16]` gathered affinities
- `token_index` (int): Index of the current token
- `dims` (MLPTKGConstantsDimensionSizes): Dimension sizes object
- `sbm` (SbufManager): SBUF memory manager

**Returns:**
- `nl.ndarray`: `[_pmax, K]` broadcasted token affinities

**Notes:**
- Computes partition and quadrant positions from token_index
- Uses `nc_stream_shuffle` for partition alignment
- Uses `stream_shuffle_broadcast` for final broadcast

## Usage Examples

### Pattern 1: CTE MoE block processing loop
```python
import nki.language as nl

TILE_SIZE = 128

def process_moe_block(block_to_expert, token_position_to_id, expert_affinities,
                       block_idx, B, E, dtype):
    """Process a single MoE block: load expert, tokens, and affinities."""
    NUM_TILES = B // TILE_SIZE

    # 1. Load which expert this block maps to
    block_expert = load_block_expert(block_to_expert, block_idx)

    # 2. Load token indices for this block
    token_indices = load_token_indices(token_position_to_id, block_idx, B, NUM_TILES)

    # 3. Calculate expert affinities for tokens in this block
    affinities = calculate_expert_affinities(
        expert_affinities_masked=expert_affinities,
        token_indices=token_indices,
        block_expert=block_expert,
        E=E,
        NUM_TILES=NUM_TILES,
        dtype=dtype,
    )

    return block_expert, token_indices, affinities
```

### Pattern 2: Dynamic block processing with skip mode
```python
import nki.language as nl

TILE_SIZE = 128

def process_dynamic_block(block_to_expert, token_position_to_id,
                            dynamic_block_idx, B, skip_invalid=True):
    """Process block with dynamic (runtime-determined) block index."""
    NUM_TILES = B // TILE_SIZE

    skip_mode = SkipMode(skip_token=skip_invalid, skip_weight=False)

    # Load expert for dynamic block
    block_expert = load_block_expert(block_to_expert, dynamic_block_idx)

    # Load token indices with skip mode for invalid tokens
    token_indices = load_token_indices_dynamic_block(
        token_position_to_id, dynamic_block_idx, B, NUM_TILES,
        skip_dma=skip_mode,
    )

    return block_expert, token_indices
```

### Pattern 3: TKG expert affinity gathering and broadcasting
```python
import nki.language as nl

def process_tkg_expert_affinities(expert_affinities_sb, expert_idx, dims, sbm):
    """Gather and broadcast expert affinities for TKG MoE."""
    # Gather affinities for all tokens based on expert indices
    gathered = gather_expert_affinities(expert_affinities_sb, expert_idx, dims, sbm)

    # Broadcast per-token affinities for each token in the sequence
    for token_idx in range(dims.T):
        dst = nl.ndarray((dims._pmax, dims.K), dtype=expert_affinities_sb.dtype, buffer=nl.sbuf)
        broadcast_token_affinity(dst, gathered, token_idx, dims, sbm)
        # Use broadcasted affinities for weighted expert computation
```

## Dependencies

- `nki.isa` (`nisa`): `dma_copy`, `dma_transpose`, `nc_stream_shuffle`, `tensor_scalar`, `tensor_tensor`, `tensor_copy`, `memset`, `local_gather`
- `nki.isa.constants`: `oob_mode` for DMA out-of-bounds handling
- `nki.language` (`nl`): Core language module for tensor allocation and operations
- `nkilib/core/utils/common_types.py`: `ActFnType`, `ExpertAffinityScaleMode` enums
- `nkilib/core/utils/kernel_assert.py`: `kernel_assert()` for validation
- `nkilib/core/utils/kernel_helpers.py`: `reduce()` for shape computation
- `nkilib/core/utils/allocator.py`: `SbufManager` for SBUF allocation (TKG path)
- `nkilib/core/utils/tensor_view.py`: `TensorView` for shape manipulation
- `nkilib/core/utils/stream_shuffle_broadcast.py`: Shared broadcast utility (TKG path)

## Full Source Implementation

```python
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# ============================================================
# CTE MoE Utilities (from moe_cte_utils.py)
# ============================================================

import nki.isa as nisa
import nki.language as nl
from nki.isa.constants import oob_mode
from nki.language import NKIObject

TILE_SIZE = 128


class SkipMode(NKIObject):
    """
    Controls DMA skipping behavior for memory optimization.

    Attributes:
        skip_token (bool): Skip DMA operations for out-of-bounds tokens (default: False)
        skip_weight (bool): Skip DMA operations for weight loading (default: False)
    """
    skip_token: bool = False
    skip_weight: bool = False

    def __init__(self, skip_token: bool = False, skip_weight: bool = False):
        self.skip_token = skip_token
        self.skip_weight = skip_weight


def stream_shuffle_broadcast(src, dst):
    """
    Broadcasts the first partition of src onto the partition dim of dst.

    All inputs and outputs are assumed to be in SBUF.
    Requires 2D src and dst, and the final dim of src matching the final dim of dst.

    Args:
        src: 2D input tensor in SBUF.
        dst: 2D output tensor in SBUF.
    """
    dst_npar = dst.shape[0]
    dst_free = dst.shape[1]

    shuffle_mask = [0] * 32
    for bank_idx in range((dst_npar + 31) // 32):
        cur_npar = min(32, dst_npar - bank_idx * 32)

        nisa.nc_stream_shuffle(
            src=src[0:1, 0:dst_free],
            dst=dst[bank_idx * 32 : bank_idx * 32 + cur_npar, 0:dst_free],
            shuffle_mask=shuffle_mask,
        )


def load_block_expert(block_to_expert, block_idx):
    """
    Load expert ID assigned to the current block.

    Args:
        block_to_expert (nl.ndarray): Mapping tensor of shape [N, 1].
        block_idx (int or nl.ndarray): Block index to load.

    Returns:
        block_expert (nl.ndarray): Expert ID tensor of shape [1, 1] in SBUF.
    """
    block_expert = nl.ndarray((1, 1), dtype=nl.int32, buffer=nl.sbuf)

    if isinstance(block_idx, int):
        nisa.dma_copy(dst=block_expert[0, 0], src=block_to_expert.ap(pattern=[[1, 1], [1, 1]], offset=block_idx))
    else:
        nisa.dma_copy(
            dst=block_expert[0, 0],
            src=block_to_expert.ap(pattern=[[1, 1], [1, 1]], offset=0, scalar_offset=block_idx, indirect_dim=0),
        )
    return block_expert


def load_token_indices(token_position_to_id, block_idx, B, NUM_TILES):
    """
    Load and transpose token indices for the current block (static block index).

    Args:
        token_position_to_id (nl.ndarray): Token position mapping of shape [N*B].
        block_idx (int): Current block index.
        B (int): Block size (number of tokens per block).
        NUM_TILES (int): Number of tiles (B // TILE_SIZE).

    Returns:
        result (nl.ndarray): Transposed token indices of shape [TILE_SIZE, NUM_TILES] in SBUF.
    """
    result = nl.ndarray((TILE_SIZE, NUM_TILES), dtype=nl.int32, buffer=nl.sbuf)
    offset = block_idx * B
    nisa.dma_transpose(
        dst=result.ap(pattern=[[NUM_TILES, TILE_SIZE], [1, 1], [1, 1], [1, NUM_TILES]]),
        src=token_position_to_id.ap(pattern=[[TILE_SIZE, NUM_TILES], [1, 1], [1, 1], [1, TILE_SIZE]], offset=offset),
    )
    return result


def load_token_indices_dynamic_block(
    token_position_to_id, block_idx, B, NUM_TILES, skip_dma: SkipMode = SkipMode(False, False)
):
    """
    Load token indices for dynamic block with runtime block index.

    Args:
        token_position_to_id (nl.ndarray): Token position mapping tensor.
        block_idx (nl.ndarray): Dynamic block index tensor.
        B (int): Block size.
        NUM_TILES (int): Number of tiles (B // TILE_SIZE).
        skip_dma (SkipMode): DMA skip configuration.

    Returns:
        local_token_indices (nl.ndarray): Token indices of shape [TILE_SIZE, NUM_TILES] in SBUF.
    """
    local_token_indices = nl.ndarray((TILE_SIZE, NUM_TILES), dtype=token_position_to_id.dtype, buffer=nl.sbuf)

    # Compute total size and reshape for block-based indexing
    total_size = 1
    for dim in token_position_to_id.shape:
        total_size = total_size * dim
    reshaped_token_position_to_id = token_position_to_id.reshape((total_size // B, B))

    block_idx_copy = nl.ndarray((1, 1), dtype=nl.int32, buffer=nl.sbuf)
    nisa.tensor_copy(block_idx_copy, block_idx)

    for idx in range(0, NUM_TILES):
        if skip_dma.skip_token:
            nisa.memset(local_token_indices[0:TILE_SIZE, idx], value=0)

        nisa.dma_copy(
            dst=local_token_indices.ap(pattern=[[NUM_TILES, TILE_SIZE], [1, 1]], offset=idx),
            src=reshaped_token_position_to_id.ap(
                pattern=[[1, TILE_SIZE], [1, 1]], offset=TILE_SIZE * idx, scalar_offset=block_idx_copy, indirect_dim=0
            ),
            oob_mode=oob_mode.skip if skip_dma.skip_token else oob_mode.error,
        )
    return local_token_indices


def calculate_expert_affinities(
    expert_affinities_masked,
    token_indices,
    block_expert,
    E,
    NUM_TILES,
    dtype,
    skip_dma: SkipMode = SkipMode(False, False),
    token_indices_offset=0,
):
    """
    Calculate expert affinities for the current block.

    Uses pointer arithmetic: addr = token_indices * E + block_expert

    Args:
        expert_affinities_masked: Expert affinities tensor of shape [(T+1)*E, 1].
        token_indices: Token indices of shape [TILE_SIZE, NUM_TILES] in SBUF.
        block_expert: Expert ID of shape [1, 1] in SBUF.
        E (int): Number of experts.
        NUM_TILES (int): Number of tiles.
        dtype: Data type for affinity values.
        skip_dma (SkipMode): DMA skip configuration.
        token_indices_offset (int): Offset for block tiling.

    Returns:
        expert_affinity_f32 (List[nl.ndarray]): List of affinity tensors, each [TILE_SIZE, 1].
    """
    v_expert = nl.ndarray((TILE_SIZE, 1), dtype=nl.int32, buffer=nl.sbuf)
    stream_shuffle_broadcast(src=block_expert, dst=v_expert)

    expert_affinity_f32 = []
    for n in range(NUM_TILES):
        expert_affinity_f32.append(nl.ndarray((TILE_SIZE, 1), dtype=nl.float32, buffer=nl.sbuf))

    for n in range(NUM_TILES):
        addr = nl.ndarray((TILE_SIZE, 1), dtype=nl.int32, buffer=nl.sbuf)
        nisa.tensor_scalar(
            dst=addr,
            data=token_indices[0:TILE_SIZE, token_indices_offset + n],
            op0=nl.multiply,
            operand0=E,
        )

        v_expert_f32 = nl.ndarray((TILE_SIZE, 1), dtype=nl.float32, buffer=nl.sbuf)
        nisa.tensor_copy(v_expert_f32, v_expert)

        addr_fin = nl.ndarray((TILE_SIZE, 1), dtype=nl.int32, buffer=nl.sbuf)
        nisa.tensor_tensor(dst=addr_fin, data1=addr, op=nl.add, data2=v_expert_f32)

        if skip_dma.skip_token:
            nisa.tensor_scalar(dst=addr_fin, data=addr_fin, op0=nl.maximum, operand0=-1)

        expert_affinity_dtype = nl.ndarray((TILE_SIZE, 1), dtype=dtype, buffer=nl.sbuf)
        if skip_dma.skip_token:
            nisa.memset(expert_affinity_dtype[0:TILE_SIZE, 0], value=0)

        expert_affinity_loaded = nl.ndarray((TILE_SIZE, 1), dtype=dtype, buffer=nl.sbuf)
        if skip_dma.skip_token:
            nisa.memset(expert_affinity_loaded, value=0)

        nisa.dma_copy(
            dst=expert_affinity_loaded,
            src=expert_affinities_masked.ap(
                pattern=[[1, TILE_SIZE], [1, 1]], offset=0, vector_offset=addr_fin, indirect_dim=0
            ),
            oob_mode=oob_mode.skip if skip_dma.skip_token else oob_mode.error,
        )

        nisa.tensor_copy(expert_affinity_f32[n][0:TILE_SIZE, 0], expert_affinity_loaded[0:TILE_SIZE, 0])

    return expert_affinity_f32
```
