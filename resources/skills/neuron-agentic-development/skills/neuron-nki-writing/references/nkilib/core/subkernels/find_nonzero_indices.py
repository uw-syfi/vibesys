# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Find nonzero indices kernel using GpSimd nonzero_with_count ISA."""

import nki
import nki.isa as nisa
import nki.language as nl
import nki.tensor as ntensor
from nki.isa import constants as nisa_constants

from ..utils.kernel_assert import kernel_assert
from ..utils.kernel_helpers import div_ceil

# Constants for GpSimd nonzero_with_count ISA
_QUADRANT_SIZE = 32  # Size of each quadrant in partition dimension
_NUM_QUADRANTS = 4  # Number of quadrants (128 / 32)
_NUM_GPSIMD_CORES = 8  # Number of GpSimd cores that process in parallel
_GPSIMD_CORES_PER_QUADRANT = 2  # GpSimd cores per quadrant
_PARTITIONS_PER_GPSIMD = 16  # Partitions between each GpSimd core (0, 16, 32, ..., 112)


@nki.jit
def find_nonzero_indices(
    input_tensor: nl.ndarray,
    col_start_id: nl.ndarray = None,
    n_cols: int = None,
    chunk_size: int = None,
    index_dtype: nki.dtype = nl.int32,
):
    """Find indices of nonzero elements along the T dimension.

    This kernel computes the indices of nonzero elements in an input tensor of shape [T, C].
    It finds indices along the T dimension for each column. The kernel is optimized
    for LNC2 sharding and uses the GpSimd nonzero_with_count ISA for efficient parallel
    processing of 8 columns at a time. Optimized for token counts up to 65536 and column
    counts up to 128.

    Dimensions:
        T: Sequence/token dimension (first dimension of input)
        C: Column dimension that used to calculate the non zero indices (second dimension of input)
        C_full: Full columns dimension from input tensor shape
        C_per_shard: Columns processed per LNC shard (C // NUM Shards)

    Args:
        input_tensor (nl.ndarray): [T, C], Input tensor on HBM. Nonzero elements are found
            along the T dimension for each column.
        col_start_id (nl.ndarray): [1], Optional HBM tensor containing the starting column
            index in the C dimension. If specified, only n_cols Columns starting from col_start_id are processed.
            If None, all C Columns are processed.
        n_cols (int): Number of columns (in C dimension) to process. Required when
            col_start_id is specified, ignored otherwise.
        chunk_size (int): Size of chunks for processing T dimension. If None, defaults to T.
            Must divide T evenly. Smaller chunk sizes reduce memory usage.
        index_dtype (nki.dtype): Data type for output indices tensor. Default is nl.int32.

    Returns:
        indices (nl.ndarray): [C, T] or [n_cols, T], Tensor containing nonzero indices.
            For each column c, the first N values are the T-indices of nonzero elements,
            followed by -1 padding values.
        nonzero_counts (nl.ndarray): [C] or [n_cols], Count of nonzero elements per column.

    Notes:
        - Requires LNC2 configuration (2 NeuronCores)
        - C must be divisible by 2 (for LNC2 sharding)
        - chunk_size must be divisible by 128 (partition size)
        - Uses GpSimd nonzero_with_count ISA which only operates on partitions [0, 16, 32, ..., 112]

    Pseudocode:
        for each column c in [0, C):
            count = 0
            for t in [0, T):
                if input_tensor[t, c] != 0:
                    indices[c, count] = t
                    count += 1
            # Pad remaining with -1
            for i in [count, T):
                indices[c, i] = -1
            nonzero_counts[c] = count
    """
    T_DIM, C_DIM = input_tensor.shape
    # Handle col_start_id parameter for processing subset of columns
    if col_start_id != None and n_cols != None:
        col_start_id_sbuf = nl.ndarray((1, 1), dtype=nl.int32, buffer=nl.sbuf, name="col_start_id_sbuf")
        nisa.dma_copy(dst=col_start_id_sbuf, src=col_start_id[0:1])
        C = n_cols
    else:
        col_start_id_sbuf = None
        C = C_DIM

    num_shards = nl.num_programs(0)
    shard_id = nl.program_id(0)
    C_per_shard = C // num_shards
    C_offset = C_per_shard * shard_id

    P_MAX = nl.tile_size.pmax  # 128
    T_TILE_SIZE = P_MAX  # Tile size for the T (token/sequence) dimension
    C_TILE_SIZE = P_MAX  # Tile size for the C dimension / SBUF partition count

    # Use chunk_size to limit SBUF usage for large T
    if chunk_size == None:
        chunk_size = T_DIM
    kernel_assert(T_DIM % chunk_size == 0, f"T_DIM ({T_DIM}) must be divisible by chunk_size ({chunk_size})")
    CHUNK_T_TILES = chunk_size // T_TILE_SIZE
    NUM_CHUNKS = T_DIM // chunk_size

    # Allocate output tensors
    indices = nl.ndarray((C, T_DIM), dtype=index_dtype, buffer=nl.shared_hbm)

    # Initialize indices to -1 when processing in chunks (partial writes need padding)
    if NUM_CHUNKS > 1:
        sbuf_init = nl.ndarray(
            (P_MAX, C_per_shard * T_DIM // P_MAX), dtype=index_dtype, buffer=nl.sbuf, name="sbuf_init"
        )
        nisa.memset(dst=sbuf_init, value=-1)
        reshaped_dst = indices.reshape((P_MAX * 2, C_per_shard * T_DIM // P_MAX))
        nisa.dma_copy(dst=reshaped_dst[P_MAX * shard_id : P_MAX * (shard_id + 1), :], src=sbuf_init)

    nonzero_counts = nl.ndarray((C,), dtype=nl.int32, buffer=nl.shared_hbm)
    nonzero_counts_local = nl.ndarray((1, C_per_shard), dtype=nl.int32, buffer=nl.sbuf, name="nonzero_counts_local")
    nisa.memset(dst=nonzero_counts_local, value=0)

    # Calculate iteration counts
    n_column_rounds = div_ceil(C_per_shard, _NUM_GPSIMD_CORES)

    # Identity matrix for nc_matmul transpose
    identity_hbm = nl.shared_constant(ntensor.identity(P_MAX, nl.int8))
    identity_sb = nl.ndarray((P_MAX, P_MAX), dtype=nl.float32, buffer=nl.sbuf, name="identity_sb")
    nisa.dma_copy(dst=identity_sb, src=identity_hbm)

    for column_round_idx in range(n_column_rounds):
        n_columns_this_round = min(_NUM_GPSIMD_CORES, C_per_shard - _NUM_GPSIMD_CORES * column_round_idx)
        column_start_offset = column_round_idx * _NUM_GPSIMD_CORES + C_offset

        # Track cumulative offsets for writing indices
        offsets = nl.ndarray(
            (1, _NUM_GPSIMD_CORES), dtype=nl.int32, buffer=nl.sbuf, name=f"offsets_er-{column_round_idx}"
        )
        nisa.memset(dst=offsets, value=0)
        for chunk_idx in range(NUM_CHUNKS):
            input_sbuf = nl.ndarray(
                (T_TILE_SIZE, CHUNK_T_TILES, _NUM_GPSIMD_CORES),
                dtype=input_tensor.dtype,
                buffer=nl.sbuf,
            )
            input_gpsimd_aligned_sbuf = nl.ndarray(
                (T_TILE_SIZE, CHUNK_T_TILES, C_TILE_SIZE),
                dtype=nl.float32,
                buffer=nl.sbuf,
            )
            input_gpsimd_aligned_transposed_sbuf = nl.ndarray(
                (C_TILE_SIZE, CHUNK_T_TILES, T_TILE_SIZE),
                dtype=input_tensor.dtype,
                buffer=nl.sbuf,
            )
            indices_sbuf = nl.ndarray((C_TILE_SIZE, 1, chunk_size + 1), dtype=nl.int32, buffer=nl.sbuf)
            t_chunk_start = chunk_idx * chunk_size

            # --- Load phase: single DMA copy for all T tiles in the chunk ---
            if col_start_id_sbuf != None:
                nisa.dma_copy(
                    dst=input_sbuf[:, 0:CHUNK_T_TILES, 0:n_columns_this_round],
                    src=input_tensor.ap(
                        pattern=[[C_DIM, T_TILE_SIZE], [C_DIM * T_TILE_SIZE, CHUNK_T_TILES], [1, n_columns_this_round]],
                        offset=column_start_offset + (t_chunk_start * C_DIM),
                        scalar_offset=col_start_id_sbuf,
                        indirect_dim=1,
                    ),
                    dge_mode=nisa_constants.dge_mode.hwdge,
                )
            else:
                nisa.dma_copy(
                    dst=input_sbuf[:, 0:CHUNK_T_TILES, 0:n_columns_this_round],
                    src=input_tensor.ap(
                        pattern=[[C_DIM, T_TILE_SIZE], [C_DIM * T_TILE_SIZE, CHUNK_T_TILES], [1, n_columns_this_round]],
                        offset=column_start_offset + (t_chunk_start * C_DIM),
                    ),
                )

            # --- Scatter phase: columns to partitions 0, 16, 32, ..., 112 for GpSimd ---
            for column_idx in range(n_columns_this_round):
                nisa.tensor_copy(
                    dst=input_gpsimd_aligned_sbuf[:, :, column_idx * _PARTITIONS_PER_GPSIMD],
                    src=input_sbuf[:, :, column_idx],
                    engine=nisa.engine.scalar,
                )

            # --- Scatter and transpose phase: per T tile ---
            for t_tile_idx in range(CHUNK_T_TILES):
                transposed_psum = nl.ndarray((C_TILE_SIZE, T_TILE_SIZE), dtype=nl.float32, buffer=nl.psum)
                nisa.nc_matmul(
                    dst=transposed_psum,
                    stationary=input_gpsimd_aligned_sbuf[:, t_tile_idx, :],
                    moving=identity_sb[0:P_MAX, 0:P_MAX],
                    is_transpose=True,
                )
                nisa.tensor_copy(
                    dst=input_gpsimd_aligned_transposed_sbuf[:, t_tile_idx, :],
                    src=transposed_psum,
                )

            # --- nonzero_with_count ---
            nisa.nonzero_with_count(
                dst=indices_sbuf,
                src=input_gpsimd_aligned_transposed_sbuf,
                index_offset=chunk_idx * chunk_size,
                padding_val=-1,
            )

            # --- Store results: extract from even GpSimd cores (partitions 0, 32, 64, 96) ---
            for quadrant_idx in range(_NUM_QUADRANTS):
                column_idx = quadrant_idx * _GPSIMD_CORES_PER_QUADRANT
                _store_indices_and_count(
                    indices_sbuf=indices_sbuf,
                    indices=indices,
                    offsets=offsets,
                    column_round_idx=column_round_idx,
                    chunk_idx=chunk_idx,
                    quadrant_idx=quadrant_idx,
                    column_idx=column_idx,
                    n_columns_this_round=n_columns_this_round,
                    C_offset=C_offset,
                    chunk_size=chunk_size,
                    T_DIM=T_DIM,
                    name_prefix="even",
                )

            # --- Shuffle to move odd core data (partitions 16, 48, 80, 112) to readable positions ---
            quad_mask = [_PARTITIONS_PER_GPSIMD] + [255] * (_QUADRANT_SIZE - 1)
            nisa.nc_stream_shuffle(dst=indices_sbuf, src=indices_sbuf, shuffle_mask=quad_mask)

            # --- Store results: extract from odd GpSimd cores ---
            for quadrant_idx in range(_NUM_QUADRANTS):
                column_idx = quadrant_idx * _GPSIMD_CORES_PER_QUADRANT + 1
                _store_indices_and_count(
                    indices_sbuf=indices_sbuf,
                    indices=indices,
                    offsets=offsets,
                    column_round_idx=column_round_idx,
                    chunk_idx=chunk_idx,
                    quadrant_idx=quadrant_idx,
                    column_idx=column_idx,
                    n_columns_this_round=n_columns_this_round,
                    C_offset=C_offset,
                    chunk_size=chunk_size,
                    T_DIM=T_DIM,
                    name_prefix="odd",
                )

        # Copy final accumulated counts for this round of columns
        nisa.tensor_copy(
            dst=nonzero_counts_local[
                0:1, column_round_idx * _NUM_GPSIMD_CORES : column_round_idx * _NUM_GPSIMD_CORES + n_columns_this_round
            ],
            src=offsets[0:1, 0:n_columns_this_round],
        )

    # Write nonzero counts to HBM
    nonzero_counts_reshape = nonzero_counts.reshape((1, C))
    nisa.dma_copy(dst=nonzero_counts_reshape[0:1, C_offset : C_offset + C_per_shard], src=nonzero_counts_local)

    return indices, nonzero_counts


def _store_indices_and_count(
    indices_sbuf: nl.ndarray,
    indices: nl.ndarray,
    offsets: nl.ndarray,
    column_round_idx: int,
    chunk_idx: int,
    quadrant_idx: int,
    column_idx: int,
    n_columns_this_round: int,
    C_offset: int,
    chunk_size: int,
    T_DIM: int,
    name_prefix: str,
):
    """Extract nonzero indices and count for one GpSimd core and DMA results to HBM.

    Reads the indices and count produced by nonzero_with_count from the quadrant's
    partition in indices_sbuf, DMAs the indices to the output HBM tensor at the
    correct column and offset, and accumulates the count into offsets.

    Args:
        indices_sbuf (nl.ndarray): [C_TILE_SIZE, 1, chunk_size+1], SBUF holding
            nonzero_with_count results. Last element per partition is the count.
        indices (nl.ndarray): [C, T_DIM], HBM output tensor for nonzero indices.
        offsets (nl.ndarray): [1, _NUM_GPSIMD_CORES], SBUF cumulative write offsets per column.
        column_round_idx (int): Current column round iteration index.
        chunk_idx (int): Current chunk iteration index.
        quadrant_idx (int): Quadrant index (0-3) selecting the partition to read from.
        column_idx (int): Column index within the current round for this core.
        n_columns_this_round (int): Number of active columns in this round.
        C_offset (int): Column offset for this LNC shard.
        chunk_size (int): Number of T elements per chunk.
        T_DIM (int): Full T dimension size.
        name_prefix (str): Prefix for SBUF tensor names (e.g. "even" or "odd").
    """
    if column_idx >= n_columns_this_round:
        return

    offset_tile = nl.ndarray(
        (1, 1),
        dtype=nl.int32,
        buffer=nl.sbuf,
        name=f"{name_prefix}_offset_tile_er-{column_round_idx}_ch-{chunk_idx}_qi-{quadrant_idx}",
    )
    nisa.tensor_copy(dst=offset_tile, src=offsets[0:1, column_idx : column_idx + 1])

    out_col = C_offset + column_round_idx * _NUM_GPSIMD_CORES + column_idx
    src_data = nl.ndarray(
        (1, chunk_size),
        dtype=nl.int32,
        buffer=nl.sbuf,
        name=f"{name_prefix}_src_data_er-{column_round_idx}_ch-{chunk_idx}_qi-{quadrant_idx}",
    )
    nisa.tensor_copy(
        dst=src_data,
        src=indices_sbuf[quadrant_idx * _QUADRANT_SIZE : quadrant_idx * _QUADRANT_SIZE + 1, 0, 0:chunk_size],
    )
    nisa.dma_copy(
        dst=indices.ap(
            pattern=[[T_DIM, 1], [1, chunk_size]],
            offset=out_col * T_DIM,
            scalar_offset=offset_tile,
            indirect_dim=1,
        ),
        src=src_data,
    )

    count_tile = nl.ndarray(
        (1, 1),
        dtype=nl.int32,
        buffer=nl.sbuf,
        name=f"{name_prefix}_count_tile_er-{column_round_idx}_ch-{chunk_idx}_qi-{quadrant_idx}",
    )
    nisa.tensor_copy(
        dst=count_tile,
        src=indices_sbuf[
            quadrant_idx * _QUADRANT_SIZE : quadrant_idx * _QUADRANT_SIZE + 1, 0, chunk_size : chunk_size + 1
        ],
    )
    nisa.tensor_tensor(
        dst=offsets[0:1, column_idx : column_idx + 1],
        data1=offsets[0:1, column_idx : column_idx + 1],
        data2=count_tile,
        op=nl.add,
    )
