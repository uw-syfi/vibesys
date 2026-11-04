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

"""Utility functions for normalization kernels in token generation mode."""

from typing import Optional, Tuple

import nki.isa as nisa
import nki.language as nl

from ..utils.allocator import SbufManager
from ..utils.kernel_assert import kernel_assert
from ..utils.kernel_helpers import div_ceil
from ..utils.tensor_view import TensorView
from ..utils.tiled_range import TiledRange

# DMA engine mode
_DGE_MODE_NONE = 3

# PSUM bank count for cycling allocations
_PSUM_BANK_COUNT = 8

# Threshold for using contiguous load + on-chip transpose
_CONTIGUOUS_LOAD_H_THRESHOLD = 2048

# Alignment constants for nc_transpose
_PSUM_ALIGNMENT_BYTES = 4


def validate_shapes(
    input_view: TensorView,
    gamma_view: TensorView,
    output_view: TensorView,
) -> Tuple[int, int, int, int]:
    """
    Validate tensor shapes for normalization operations.

    Args:
        input_view (TensorView): Input tensor view
        gamma_view (TensorView): Gamma tensor view
        output_view (TensorView): Output tensor view

    Returns:
        Tuple[int, int, int, int]: (BxS, H, H0, H1) dimensions

    Notes:
        - H0 must equal nl.tile_size.pmax (128)
        - H must be divisible by H0
        - Output shape must be [H0, BxS, H1]
        - Gamma shape must be [1, H]
    """
    H0 = nl.tile_size.pmax
    if input_view.is_sbuf():
        _H0, BxS, H1 = input_view.shape
        kernel_assert(
            _H0 == H0,
            f"Input tensor in SBUF does not have partition dimension H0 of {H0}, got {_H0}",
        )
        H = _H0 * H1
    else:
        B, S, H = input_view.shape
        BxS = B * S
        kernel_assert(H % H0 == 0, f"Input tensor H dimension must be divisible by {H0}, got {H}")
        H1 = H // H0

    kernel_assert(
        tuple(output_view.shape) == (H0, BxS, H1),
        f"Output shape expected is (H0, BxS, H1): {(H0, BxS, H1)}, got {tuple(output_view.shape)}",
    )

    kernel_assert(
        gamma_view.shape == (1, H),
        f"Malformed shape of gamma expected [1, {H}], got {gamma_view.shape}",
    )
    return BxS, H, H0, H1


def contiguous_load_transpose(
    input_hbm: TensorView,
    input_sb: TensorView,
    num_H_shards: int,
    sbm: SbufManager,
) -> None:
    """
    Load input using contiguous DMA + on-chip nc_transpose.

    More efficient than dma_copy for small H dimensions. Loads data contiguously
    to SBUF, then uses nc_transpose to rearrange into the target layout.

    Args:
        input_hbm (TensorView): [BxS, H], Input tensor view in HBM
        input_sb (TensorView): [H0, BxS, H1], Output buffer in SBUF
        num_H_shards (int): Number of shards along H dimension
        sbm (SbufManager): SBUF memory manager

    Returns:
        None: Data is written directly into input_sb

    Notes:
        Data Layout:
            HBM input:  [BxS, H] where H = num_H_shards * H0 * H2
                        Logical view: [BxS, num_H_shards, H0, H2] (row-major)
                        Memory order: for each bxs, data is [shard0{H0*H2}, shard1{H0*H2}, ...]
                                      within each shard: [h0_0*H2 elements, h0_1*H2 elements, ...]

            SBUF output: [H0, BxS, H1] where H1 = num_H_shards * H2
                         Logical view: [H0, BxS, num_H_shards, H2]
    """
    H0 = nl.tile_size.pmax
    _psum_fmax = nl.tile_size.psum_fmax

    BxS, H = input_hbm.shape
    H1 = H // H0
    H2 = H1 // num_H_shards

    # Reshape output [H0, BxS, H1] -> [H0, BxS, num_H_shards, H2]
    output_reshaped = input_sb.reshape_dim(dim=2, shape=[num_H_shards, H2])

    # Total (shard, h2) tiles to process per BxS tile
    total_h_tiles = num_H_shards * H2

    # PSUM alignment: compute padded size for 4-byte alignment
    dtype_size = 2 if input_hbm.dtype in [nl.float16, nl.bfloat16] else 4

    for bxs_tile in TiledRange(BxS, H0):
        # Load [bxs_tile.size, H] from HBM to SBUF
        input_sbuf_temp = sbm.alloc_heap(
            (bxs_tile.size, H), dtype=input_hbm.dtype, buffer=nl.sbuf, name=f"cont_load_transpose_buff_{bxs_tile.index}"
        )
        input_hbm_tile = input_hbm.slice(
            dim=0, start=bxs_tile.start_offset, end=bxs_tile.end_offset
        )  # [bxs_tile.size, H]
        nisa.dma_copy(src=input_hbm_tile.get_view(), dst=input_sbuf_temp, dge_mode=_DGE_MODE_NONE)

        # Reshape [bxs_tile.size, H] -> [bxs_tile.size, num_H_shards, H0, H2]
        input_temp_view = TensorView(input_sbuf_temp).reshape_dim(dim=1, shape=[num_H_shards, H0, H2])

        # Compute padded tile size for PSUM alignment
        padded_tile_size = (
            div_ceil(bxs_tile.size * dtype_size, _PSUM_ALIGNMENT_BYTES) * _PSUM_ALIGNMENT_BYTES // dtype_size
        )

        tiles_per_psum = _psum_fmax // padded_tile_size

        for psum_tile in TiledRange(total_h_tiles, tiles_per_psum):
            psum_bank_idx = psum_tile.index % _PSUM_BANK_COUNT
            tiles_this_psum = psum_tile.size

            # Allocate PSUM [H0, tiles_this_psum * padded_tile_size]
            tp_psum = nl.ndarray(
                (H0, tiles_this_psum * padded_tile_size),
                dtype=input_hbm.dtype,
                buffer=nl.psum,
                address=None if sbm.is_auto_alloc() else (0, psum_bank_idx * _psum_fmax * 4),
            )

            # Transpose each (shard, h2) tile into PSUM
            for tile_in_psum in range(tiles_this_psum):
                h_tile_idx = psum_tile.start_offset + tile_in_psum
                shard_idx, h2_idx = divmod(h_tile_idx, H2)
                col_offset = tile_in_psum * padded_tile_size

                # Extract [bxs_tile.size, H0] for this (shard, h2)
                src_view = (
                    input_temp_view.slice(dim=1, start=shard_idx, end=shard_idx + 1)
                    .squeeze_dim(dim=1)
                    .slice(dim=2, start=h2_idx, end=h2_idx + 1)
                    .squeeze_dim(dim=2)
                )
                # Transpose [bxs_tile.size, H0] -> [H0, bxs_tile.size] in PSUM
                nisa.nc_transpose(dst=tp_psum[0:H0, col_offset : col_offset + bxs_tile.size], data=src_view.get_view())

            # Copy PSUM -> SBUF
            dst_view = (
                output_reshaped.slice(dim=1, start=bxs_tile.start_offset, end=bxs_tile.end_offset)
                .flatten_dims(start_dim=2, end_dim=3)  # [H0, bxs_tile.size, H1]
                .permute(dims=[0, 2, 1])  # [H0, H1, bxs_tile.size]
                .slice(dim=1, start=psum_tile.start_offset, end=psum_tile.end_offset)
            )

            # Create PSUM view that skips padding
            tp_psum_view = (
                TensorView(tp_psum)
                .reshape_dim(dim=1, shape=[tiles_this_psum, padded_tile_size])
                .slice(dim=2, start=0, end=bxs_tile.size)
            )
            nisa.tensor_copy(dst=dst_view.get_view(), src=tp_psum_view.get_view())

        sbm.pop_heap()


def load_input_to_sbuf(
    input_hbm: TensorView,
    input_sb: TensorView,
    num_H_shards: int,
    hidden_dim_tp: bool = False,
    sbm: Optional[SbufManager] = None,
) -> TensorView:
    """
    Load input data from HBM to SBUF with appropriate layout transformation.

    Args:
        input_hbm (TensorView): [BxS, H], Input tensor view in HBM
        input_sb (TensorView): [H0, BxS, H1], Input buffer in SBUF
        num_H_shards (int): Number of shards along H dimension
        hidden_dim_tp (bool): If True, use transpose load for (H/128, 128) layout
        sbm (Optional[SbufManager]): SBUF manager, required for contiguous load path

    Returns:
        TensorView: Input tensor view in SBUF with shape [H0, BxS, H1]

    Notes:
        - hidden_dim_tp=True: Transpose load (BxS, H) -> (BxS*H1, H0) -> (H0, BxS, H1)
        - hidden_dim_tp=False: Standard layout (BxS, H) -> (BxS, num_H_shards, H0, H2) -> (H0, BxS, num_H_shards, H2)
        - Contiguous load: Contiguous DMA + on-chip nc_transpose (more efficient for small H)
    """
    H0 = nl.tile_size.pmax
    BxS, H = input_hbm.shape
    H1 = H // H0
    H2 = H1 // num_H_shards

    use_contiguous_load = H <= _CONTIGUOUS_LOAD_H_THRESHOLD

    if hidden_dim_tp:
        # (BxS, H) -> (BxS*H1, H0) -> (H0, BxS, H1)
        input_hbm_view = (
            input_hbm.reshape_dim(dim=1, shape=[H1, H0])
            .flatten_dims(start_dim=0, end_dim=1)
            .expand_dim(dim=1)
            .expand_dim(dim=1)
        )
        input_sb_view = input_sb.flatten_dims(start_dim=1, end_dim=2).expand_dim(dim=1).expand_dim(dim=1)
        nisa.dma_transpose(dst=input_sb_view.get_view(), src=input_hbm_view.get_view())
    else:
        # (BxS, H) -> (BxS, num_H_shards, H0, H2) -> (H0, BxS, num_H_shards, H2)
        if use_contiguous_load:
            kernel_assert(sbm != None, "sbm required for contiguous load path")
            contiguous_load_transpose(input_hbm, input_sb, num_H_shards, sbm)
        else:
            input_hbm_view = input_hbm.reshape_dim(dim=1, shape=[num_H_shards, H0, H2]).permute(dims=[2, 0, 1, 3])
            input_sb_view = input_sb.reshape_dim(dim=2, shape=[num_H_shards, H2])  # (H0, BxS, num_H_shards, H2)
            nisa.dma_copy(
                dst=input_sb_view.get_view(),
                src=input_hbm_view.get_view(),
                dge_mode=_DGE_MODE_NONE,
            )
    return input_sb


def load_gamma_to_sbuf(
    gamma_hbm: TensorView,
    gamma_sb: TensorView,
    num_H_shards: int,
    hidden_dim_tp: bool = False,
) -> TensorView:
    """
    Load gamma weights from HBM to SBUF with appropriate layout transformation.

    Args:
        gamma_hbm (TensorView): [1, H], Gamma tensor view in HBM
        gamma_sb (TensorView): [H0, H1], Gamma buffer in SBUF
        num_H_shards (int): Number of shards along H dimension
        hidden_dim_tp (bool): If True, use transpose load for (H/128, 128) layout

    Returns:
        TensorView: Gamma tensor view in SBUF with shape [H0, H1]

    Notes:
        - hidden_dim_tp=True: Transpose load (H) -> (H1, H0) -> (H0, H1)
        - hidden_dim_tp=False: Standard layout (H) -> (num_H_shards, H0, H2) -> (H0, num_H_shards, H2)
    """
    H0 = nl.tile_size.pmax
    H = gamma_hbm.shape[1]
    H1 = H // H0
    H2 = H1 // num_H_shards

    # (1, H) -> (H)
    gamma_hbm = gamma_hbm.flatten_dims(start_dim=0, end_dim=1)
    if hidden_dim_tp:
        # Transpose load: (H) -> (H1, H0) -> (H0, H1)
        gamma_hbm_view = gamma_hbm.reshape_dim(dim=0, shape=[H1, H0]).expand_dim(dim=1).expand_dim(dim=1)
        gamma_sb_dst_view = gamma_sb.expand_dim(dim=1).expand_dim(dim=1)
        nisa.dma_transpose(dst=gamma_sb_dst_view.get_view(), src=gamma_hbm_view.get_view())
    else:
        # Standard layout: (H) -> (num_H_shards, H0, H2) -> (H0, num_H_shards, H2)
        gamma_hbm_view = gamma_hbm.reshape_dim(dim=0, shape=[num_H_shards, H0, H2]).permute(dims=[1, 0, 2])
        gamma_sb_view_reshaped = gamma_sb.reshape_dim(dim=1, shape=[num_H_shards, H2])
        nisa.dma_copy(
            dst=gamma_sb_view_reshaped.get_view(),
            src=gamma_hbm_view.get_view(),
            dge_mode=_DGE_MODE_NONE,
        )
    return gamma_sb


def get_token_tile_size(num_tokens: int) -> int:
    """
    Determine tile size for processing tokens in RMSNorm quantize MX TKG kernel.

    Finds the largest power-of-2 tile size (8-64) that evenly divides num_tokens.
    Used to tile the token (batch * sequence) dimension for efficient processing.

    Args:
        num_tokens (int): Number of tokens to process (typically BxS // num_shards)

    Returns:
        int: Tile size for iterating over tokens

    Notes:
        - Falls back to num_tokens if no power-of-2 tile size in [8, 64] divides evenly
    """
    MIN_TILE_SIZE = 8
    MAX_TILE_SIZE = 64

    tile_size = MAX_TILE_SIZE
    while tile_size >= MIN_TILE_SIZE:
        if num_tokens % tile_size == 0:
            return tile_size
        tile_size //= 2

    return num_tokens


def validate_shapes_quantize_mx(
    input_shape: tuple,
    gamma_shape: tuple,
    output_shape: tuple,
    output_quant_shape: tuple,
    output_scale_shape: tuple,
    output_dtype,
    hidden_dim_tp: bool,
    is_residual_add: bool,
    residual_shape: tuple = None,
    output_residual_shape: tuple = None,
) -> tuple:
    """
    Validate tensor shapes for RMSNorm + MX quantization operations.

    Args:
        input_shape: Input tensor shape [B, S, H]
        gamma_shape: Gamma tensor shape [1, H]
        output_shape: Output tensor shape [H0, B*S, H1]
        output_quant_shape: Quantized output shape [H0, H/512, B*S]
        output_scale_shape: Scale output shape [H0, H/512, B*S]
        output_dtype: Output tensor dtype
        hidden_dim_tp: Whether H dimension is transposed
        is_residual_add: Whether residual add is enabled
        residual_shape: Residual tensor shape (if is_residual_add)
        output_residual_shape: Output residual shape (if is_residual_add)

    Returns:
        Tuple of (B, S, H, H0, H1, BxS, n_H512_tiles)
    """
    H0 = nl.tile_size.pmax
    B, S, H = input_shape
    H1 = H // H0
    BxS = B * S
    n_H512_tiles = H // 512

    kernel_assert(H % 512 == 0, f"Expected H divisible by 512, got H={H}")
    kernel_assert(H % H0 == 0, f"H must be divisible by {H0}")
    kernel_assert(gamma_shape == (1, H), f"Malformed shape of gamma {gamma_shape}")
    kernel_assert(hidden_dim_tp, "Only hidden_dim_tp=True is supported")
    kernel_assert(output_shape == (H0, BxS, H1), f"Expected output.shape = {(H0, BxS, H1)}")

    SUPPORTED_QMX_INPUT_DTYPES = [nl.float16, nl.bfloat16]
    kernel_assert(output_dtype in SUPPORTED_QMX_INPUT_DTYPES, "output.dtype must be float16 or bfloat16")

    if is_residual_add:
        kernel_assert(input_shape == residual_shape, "input and residual shapes must match")
        kernel_assert(output_residual_shape is not None, "output_residual required when residual provided")
        kernel_assert(output_residual_shape == (BxS, H), f"expected output_residual shape (B*S, H)={(BxS, H)}")
        kernel_assert(H1 % 8 == 0, f"Expected H1 divisible by 8 with fused residual add")
        # Residual transpose requires shard_size >= 128 (with LNC=2, BxS >= 256)
        kernel_assert(BxS >= 256, f"Residual add requires BxS >= 256 (got {BxS})")
    else:
        kernel_assert(H1 % 4 == 0, f"Expected H1 divisible by 4")

    return B, S, H, H0, H1, BxS, n_H512_tiles
