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

"""Fused optional residual add + RMSNorm + MX quantization kernel optimized for token generation (decoding) phase."""

from typing import Optional

import nki.isa as nisa
import nki.language as nl

from ..mlp.mlp_tkg.projection_mx_constants import _q_width
from ..utils.kernel_assert import kernel_assert
from ..utils.kernel_helpers import div_ceil, get_verified_program_sharding_info
from ..utils.tensor_view import TensorView
from .norm_tkg_utils import get_token_tile_size, validate_shapes_quantize_mx


def rmsnorm_mx_quantize_tkg(
    input: nl.ndarray,
    gamma: nl.ndarray,
    output: nl.ndarray,
    output_quant: nl.ndarray,
    output_scale: nl.ndarray,
    residual: Optional[nl.ndarray] = None,
    output_residual: Optional[nl.ndarray] = None,
    eps: float = 1e-6,
    hidden_actual: Optional[int] = None,
    hidden_dim_tp: bool = True,
):
    """
    Fused residual add (optional) + RMSNorm + MX quantization for token generation.

    Dimensions:
        B: Batch size
        S: Sequence length
        H: Hidden dimension size
        H0: Partition dimension (128)
        H1: H // H0

    Args:
        input (nl.ndarray): [B, S, H], Input tensor on HBM.
        gamma (nl.ndarray): [1, H], RMSNorm scaling weights on HBM.
        output (nl.ndarray): [H0, B*S, H1], FP16/BF16 output tensor in SBUF.
        output_quant (nl.ndarray): [H0, H/512, B*S], FP8x4 quantized output tensor in SBUF.
        output_scale (nl.ndarray): [H0, H/512, B*S], MX scale output tensor in SBUF.
        residual (Optional[nl.ndarray]): [B, S, H], Optional residual tensor for fused add on HBM.
        output_residual (Optional[nl.ndarray]): [B*S, H], Optional output for residual add result on HBM.
        eps (float): Epsilon for numerical stability. Default is 1e-6.
        hidden_actual (Optional[int]): Actual hidden dimension for padded inputs. Default is None (uses H).
        hidden_dim_tp (bool): If True, H dimension view is (H/128, 128). Default is True.

    Returns:
        Tuple of (output, output_quant, output_scale) or
        (output, output_quant, output_scale, output_residual) if residual is provided.

    Notes:
        - Requires LNC=2 sharding configuration
        - BxS must be divisible by 4 for LNC2 sharding alignment
        - Output tensors must be pre-allocated in SBUF

    Pseudocode:
        # For each token tile:
        hidden = input + residual  # if residual provided
        squared = hidden ** 2
        rms = sqrt(mean(squared) + eps)
        normalized = (hidden * gamma) / rms
        output = normalized
        output_quant, output_scale = quantize_mx(output)
        # Gather results across LNC cores
    """
    # Configuration
    pmax = H0 = nl.tile_size.pmax
    psum_fmax = nl.tile_size.gemm_moving_fmax
    is_residual_add = residual != None
    inter_dtype = nl.float32

    # Step 1: Validate shapes
    B, S, H, H0, H1, BxS, num_H512_tiles = validate_shapes_quantize_mx(
        input_shape=input.shape,
        gamma_shape=gamma.shape,
        output_shape=output.shape,
        output_quant_shape=output_quant.shape,
        output_scale_shape=output_scale.shape,
        output_dtype=output.dtype,
        hidden_dim_tp=hidden_dim_tp,
        is_residual_add=is_residual_add,
        residual_shape=residual.shape if is_residual_add else None,
        output_residual_shape=output_residual.shape if output_residual != None else None,
    )

    if hidden_actual == None:
        hidden_actual = H

    # Step 2: LNC sharding setup
    _, num_shards, shard_id = get_verified_program_sharding_info("rmsnorm_mx_quantize_tkg", (0, 1))
    kernel_assert(num_shards == 2, "rmsnorm_mx_quantize_tkg kernel only supports LNC=2")
    kernel_assert(BxS % 4 == 0, f"BxS must be divisible by 4")  # Required for LNC2 sharding alignment

    shard_size = BxS // num_shards
    BxS_offset = shard_id * shard_size

    # Step 3: Allocate buffers and load constants
    residual_sb = nl.ndarray((H0, shard_size, H1), dtype=residual.dtype, buffer=nl.sbuf) if is_residual_add else None
    gamma_sb = nl.ndarray((H0, H1), dtype=gamma.dtype, buffer=nl.sbuf)
    zero_bias = nl.ndarray((H0, 1), dtype=inter_dtype, buffer=nl.sbuf)
    reduction_const_matrix = nl.ndarray((H0, H0), dtype=inter_dtype, buffer=nl.sbuf)
    eps_loaded = nl.ndarray((H0, 1), dtype=inter_dtype, buffer=nl.sbuf)

    nisa.memset(zero_bias, value=0.0)
    nisa.memset(reduction_const_matrix, value=1.0)
    nisa.memset(eps_loaded, value=eps)

    # Load gamma with transpose: (1, H) -> (H) -> (H1, H0) -> (H0, H1)
    gamma_hbm = TensorView(gamma).flatten_dims(start_dim=0, end_dim=1)
    gamma_hbm_view = gamma_hbm.reshape_dim(dim=0, shape=[H1, H0]).expand_dim(dim=1).expand_dim(dim=1)
    gamma_sb_view = TensorView(gamma_sb).expand_dim(dim=1).expand_dim(dim=1)
    nisa.dma_transpose(dst=gamma_sb_view.get_view(), src=gamma_hbm_view.get_view())

    # Tiling strategy
    BxS_tile_size = get_token_tile_size(shard_size)
    num_BxS_tiles = shard_size // BxS_tile_size
    kernel_assert(shard_size % BxS_tile_size == 0, "shard_size must be divisible by BxS_tile_size")

    # Reshape HBM views for loading with TensorView
    residual_hbm_view = TensorView(residual.reshape((B * S * H1, H0))) if is_residual_add else None
    input_hbm_view = TensorView(input.reshape((B * S * H1, H0)))

    # Step 4: Process tiles - Residual Add + RMSNorm + MX Quantization
    for bxs_tile_idx in nl.sequential_range(num_BxS_tiles):
        # Indexing
        tile_BxS_start_idx = bxs_tile_idx * BxS_tile_size
        tile_BxS_offset = BxS_offset + tile_BxS_start_idx
        hbm_tile_offset = tile_BxS_offset * H1  # Global offset into HBM
        residual_tile_BxS_slice = nl.ds(tile_BxS_start_idx, BxS_tile_size)
        output_tile_BxS_slice = nl.ds(tile_BxS_offset, BxS_tile_size)

        # Allocate tile buffers
        input_tile_sb = nl.ndarray((H0, BxS_tile_size, H1), dtype=input.dtype, buffer=nl.sbuf)
        square = nl.ndarray((H0, BxS_tile_size, H1), dtype=inter_dtype, buffer=nl.sbuf)
        reduced = nl.ndarray((H0, BxS_tile_size), dtype=inter_dtype, buffer=nl.sbuf)
        final_reduced = nl.ndarray((H0, BxS_tile_size), dtype=nl.float32, buffer=nl.psum)
        sqrt = nl.ndarray((H0, BxS_tile_size), dtype=inter_dtype, buffer=nl.sbuf)
        output_tile_swizzled = nl.ndarray(
            (H0, num_H512_tiles, BxS_tile_size, _q_width), dtype=output.dtype, buffer=nl.sbuf
        )

        """
        TensorView for dma_transpose: (BxS_tile_size * H1, H0) -> (H0, BxS_tile_size, H1).
        src: (BxS*H1, H0) -> slice -> (BxS_tile_size*H1, 1, 1, H0)
        dst: (H0, BxS_tile_size, H1) -> flatten -> (H0, 1, 1, BxS_tile_size*H1)
        """
        input_src_view = (
            input_hbm_view.slice(dim=0, start=hbm_tile_offset, end=hbm_tile_offset + BxS_tile_size * H1)
            .expand_dim(dim=1)
            .expand_dim(dim=1)
        )
        input_dst_view = (
            TensorView(input_tile_sb).flatten_dims(start_dim=1, end_dim=2).expand_dim(dim=1).expand_dim(dim=1)
        )

        if is_residual_add:
            # Transpose load residual
            residual_src_view = (
                residual_hbm_view.slice(dim=0, start=hbm_tile_offset, end=hbm_tile_offset + BxS_tile_size * H1)
                .expand_dim(dim=1)
                .expand_dim(dim=1)
            )
            residual_dst_view = (
                TensorView(residual_sb)
                .slice(dim=1, start=tile_BxS_start_idx, end=tile_BxS_start_idx + BxS_tile_size)
                .flatten_dims(start_dim=1, end_dim=2)
                .expand_dim(dim=1)
                .expand_dim(dim=1)
            )
            nisa.dma_transpose(dst=residual_dst_view.get_view(), src=residual_src_view.get_view())

            # Transpose load input
            nisa.dma_transpose(dst=input_dst_view.get_view(), src=input_src_view.get_view())

            # Residual add: hidden = input + residual
            nisa.tensor_tensor(
                residual_sb[:, residual_tile_BxS_slice, :],
                input_tile_sb,
                residual_sb[:, residual_tile_BxS_slice, :],
                nl.add,
            )

            # Input ^2
            nisa.activation(dst=square, op=nl.square, data=residual_sb[:, residual_tile_BxS_slice, :], bias=zero_bias)

            # Input * gamma (broadcast gamma across BxS_tile_size)
            gamma_sb_view = TensorView(gamma_sb).expand_dim(dim=1).broadcast(dim=1, size=BxS_tile_size)
            nisa.tensor_tensor(
                input_tile_sb,
                residual_sb[:, residual_tile_BxS_slice, :],
                gamma_sb_view.get_view(),
                nl.multiply,
            )
        else:
            # Transpose load input
            nisa.dma_transpose(dst=input_dst_view.get_view(), src=input_src_view.get_view())

            # Input ^2
            nisa.activation(dst=square, op=nl.square, data=input_tile_sb, bias=zero_bias)

            # Input * gamma (broadcast gamma across BxS_tile_size)
            gamma_sb_view = TensorView(gamma_sb).expand_dim(dim=1).broadcast(dim=1, size=BxS_tile_size)
            nisa.tensor_tensor(
                input_tile_sb,
                input_tile_sb,
                gamma_sb_view.get_view(),
                nl.multiply,
            )

        # Reduce squared input along H1 dimension (last free dimension)
        nisa.tensor_reduce(dst=reduced, op=nl.add, data=square, axis=1)

        # Complete reduction across H0 dimension using matmul
        nisa.nc_matmul(dst=final_reduced, stationary=reduction_const_matrix, moving=reduced)

        # Compute 1/sqrt(mean(x^2) + eps)
        nisa.activation(dst=sqrt, op=nl.rsqrt, data=final_reduced, scale=(1.0 / hidden_actual), bias=eps_loaded)

        # Compute input * 1/RMS(input)
        sqrt_view = TensorView(sqrt).expand_dim(dim=2).broadcast(dim=2, size=H1)
        nisa.tensor_tensor(
            output[:, output_tile_BxS_slice, :],
            input_tile_sb,
            sqrt_view.get_view(),
            nl.multiply,
        )

        # Swizzle from [H0, BxS_tile_size, H1] to [H0, num_H512_tiles, BxS_tile_size, _q_width]
        for h512_tile_idx in nl.affine_range(num_H512_tiles):
            for q_idx in nl.affine_range(_q_width):
                nisa.tensor_copy(
                    dst=output_tile_swizzled[0:H0, h512_tile_idx, 0:BxS_tile_size, q_idx],
                    src=output[0:H0, output_tile_BxS_slice, q_idx * num_H512_tiles + h512_tile_idx],
                )

        # Quantize to MXFP8
        for h512_tile_idx in nl.sequential_range(num_H512_tiles):
            nisa.quantize_mx(
                src=output_tile_swizzled[0:H0, h512_tile_idx : h512_tile_idx + 1, 0:BxS_tile_size, 0:_q_width],
                dst=output_quant[0:H0, h512_tile_idx : h512_tile_idx + 1, output_tile_BxS_slice],
                dst_scale=output_scale[0:H0, h512_tile_idx : h512_tile_idx + 1, output_tile_BxS_slice],
            )

    # Step 5: Gather output_quant and output_scale across LNC cores
    send_to_rank = recv_from_rank = 1 - shard_id
    nisa.sendrecv(
        send_to_rank=send_to_rank,
        recv_from_rank=recv_from_rank,
        src=output_quant[0:H0, 0:num_H512_tiles, nl.ds(BxS_offset, shard_size)],
        dst=output_quant[0:H0, 0:num_H512_tiles, nl.ds((1 - shard_id) * shard_size, shard_size)],
        pipe_id=0,
    )
    nisa.sendrecv(
        send_to_rank=send_to_rank,
        recv_from_rank=recv_from_rank,
        src=output_scale[0:H0, 0:num_H512_tiles, nl.ds(BxS_offset, shard_size)],
        dst=output_scale[0:H0, 0:num_H512_tiles, nl.ds((1 - shard_id) * shard_size, shard_size)],
        pipe_id=1,
    )

    # Step 6: Spill residual result to HBM (if residual add enabled)
    if is_residual_add:
        # Step 6.1: PE transpose [H0, shard_size, H1] -> [pmax, num_pmax_token_tiles, H]
        num_pmax_token_tiles = div_ceil(shard_size, pmax)
        residual_dtype = residual_sb.dtype
        is_16bit = residual_dtype in [nl.float16, nl.bfloat16]
        transpose_tile_H = psum_fmax * 2 if is_16bit else psum_fmax
        num_transpose_tiles = num_H512_tiles // 2 if is_16bit else num_H512_tiles
        num_H1_per_transpose_tile = transpose_tile_H // H0

        """
        PE transpose residual_sb from [H0, shard_size, H1] to [pmax, num_pmax_token_tiles, H].

        residual_sb actual layout: (H0, shard_size * H1)
        To access [0:H0, pmax_token_tile_idx*pmax : pmax_token_tile_idx*pmax+tile_tokens_actual, H1_idx]
        as (H0, tile_tokens_actual): element [p, t, h1] is at position p * (shard_size * H1) + t * H1 + h1
        AP: [[shard_size*H1, H0], [H1, tile_tokens_actual]], offset = pmax_token_tile_idx * pmax * H1 + H1_idx
        """

        residual_transposed_sb = nl.ndarray((pmax, num_pmax_token_tiles, H), dtype=residual_dtype, buffer=nl.sbuf)
        for pmax_token_tile_idx in nl.affine_range(num_pmax_token_tiles):
            tile_tokens_actual = min(pmax, shard_size - pmax_token_tile_idx * pmax)
            residual_sb_ap = [[shard_size * H1, H0], [H1, tile_tokens_actual]]
            for transpose_tile_idx in nl.affine_range(num_transpose_tiles):
                residual_transposed_tile_psum = nl.ndarray(
                    (pmax, transpose_tile_H), dtype=residual_dtype, buffer=nl.psum
                )
                for h1_in_tile_idx in nl.affine_range(num_H1_per_transpose_tile):
                    H1_idx = transpose_tile_idx * num_H1_per_transpose_tile + h1_in_tile_idx
                    nisa.nc_transpose(
                        dst=residual_transposed_tile_psum[0:tile_tokens_actual, nl.ds(h1_in_tile_idx * H0, H0)],
                        data=residual_sb.ap(residual_sb_ap, offset=pmax_token_tile_idx * pmax * H1 + H1_idx),
                    )
                # TODO: fine tune the tensor_copy to allow both DVE and ACT engines to perform the copy
                nisa.tensor_copy(
                    dst=residual_transposed_sb[
                        0:tile_tokens_actual,
                        pmax_token_tile_idx,
                        nl.ds(transpose_tile_idx * transpose_tile_H, transpose_tile_H),
                    ],
                    src=residual_transposed_tile_psum[0:tile_tokens_actual, 0:transpose_tile_H],
                )

        """
        Spill transposed residual [pmax, num_pmax_token_tiles, H]@SB -> [shard_size, H]@HBM.

        residual_transposed_sb shape: (pmax, num_pmax_token_tiles, H), actual layout: (pmax, num_pmax_token_tiles * H)
        output_residual shape: (BxS, H)

        TODO: this vectorized DMA transfer may not achieve the best perf compared to fusing the tiled DMA transfers
        into the above loop to be overlapped with the transpose. Need to fine-tune.
        """
        num_full_tiles = shard_size // pmax
        remainder = shard_size % pmax

        if remainder == 0 and num_full_tiles > 1:
            """
            All tiles are full and multiple tiles - one vectorized DMA with 3D AP.
            SBUF: (128, N, H) -> AP: [[N*H, 128], [H, N], [1, H]]
            DRAM: (shard_size, H) -> AP: [[H, 128], [128*H, N], [1, H]]
            """
            src_ap = [[num_full_tiles * H, pmax], [H, num_full_tiles], [1, H]]
            dst_ap = [[H, pmax], [pmax * H, num_full_tiles], [1, H]]
            nisa.dma_copy(
                src=residual_transposed_sb.ap(src_ap),
                dst=output_residual.ap(dst_ap, offset=BxS_offset * H),
            )
        elif remainder == 0:
            # Single full tile
            nisa.dma_copy(
                src=residual_transposed_sb[0:pmax, 0, 0:H],
                dst=output_residual[nl.ds(BxS_offset, pmax), 0:H],
            )
        else:
            # Has partial last tile
            if num_full_tiles > 1:
                # Vectorized DMA for full tiles with 3D AP
                src_ap = [[num_full_tiles * H, pmax], [H, num_full_tiles], [1, H]]
                dst_ap = [[H, pmax], [pmax * H, num_full_tiles], [1, H]]
                nisa.dma_copy(
                    src=residual_transposed_sb.ap(src_ap),
                    dst=output_residual.ap(dst_ap, offset=BxS_offset * H),
                )
            elif num_full_tiles == 1:
                # Single full tile
                nisa.dma_copy(
                    src=residual_transposed_sb[0:pmax, 0, 0:H],
                    dst=output_residual[nl.ds(BxS_offset, pmax), 0:H],
                )
            # Single DMA for partial last tile
            nisa.dma_copy(
                src=residual_transposed_sb[0:remainder, num_full_tiles, 0:H],
                dst=output_residual[nl.ds(BxS_offset + num_full_tiles * pmax, remainder), 0:H],
            )

        return output, output_quant, output_scale, output_residual
    else:
        return output, output_quant, output_scale
