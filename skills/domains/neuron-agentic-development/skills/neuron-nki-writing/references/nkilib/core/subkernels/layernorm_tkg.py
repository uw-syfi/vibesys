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

"""LayerNorm subkernel optimized for token generation (TKG) inference with LNC sharding support."""

from typing import Optional

import nki.isa as nisa
import nki.language as nl

from ..utils.allocator import SbufManager
from ..utils.kernel_assert import kernel_assert
from ..utils.kernel_helpers import get_verified_program_sharding_info
from ..utils.logging import get_logger
from ..utils.tensor_view import TensorView

# Heuristic threshold for sharding on BxS to halve computation at the cost of extra local collective
SHARDING_THRESHOLD = 10

# TODO: workaround for NKI-395
_DGE_MODE_NONE = 3


def layernorm_tkg(
    input: nl.ndarray,
    gamma: nl.ndarray,
    output: nl.ndarray,
    beta: Optional[nl.ndarray] = None,
    eps: float = 1e-6,
    use_heap_memory: bool = False,
    sbm: Optional[SbufManager] = None,
):
    """
    LayerNorm implementation optimized for inference token generation (decoding) phase.

    The output layout is specifically chosen to make the subsequent sharded matmul
    efficient for LNC > 1 case. TODO: Specify intended usage range (e.g., sequence length, batch size)

    Dimensions:
        B: Batch size
        S: Sequence length
        H: Hidden dimension size
        H0: Partition dimension (128)
        H1: H // H0

    Args:
        input (nl.ndarray): [B, S, H], Input tensor in HBM, or [H0, BxS, H1] if already in SBUF.
        gamma (nl.ndarray): [1, H], Gamma tensor used in normalization, in HBM.
        output (nl.ndarray): [H0, BxS, H1], Output tensor buffer.
        beta (nl.ndarray): Optional. [1, H], Beta tensor used in normalization, in HBM.
        eps (float): Epsilon to maintain numerical stability.
        use_heap_memory (bool): Indicates whether to allocate memory on the heap instead of the stack.
        sbm (SbufManager): Optional. Instance of SbufManager responsible for handling SBUF allocation.

    Returns:
        output (nl.ndarray): [H0, BxS, H1], Normalized output tensor.

    Notes:
        - H must be divisible by 128 (partition dimension).
        - When LNC=2 and BxS > SHARDING_THRESHOLD, computation is sharded across cores.
        - Output layout is transposed for efficient downstream sharded matmul.

    Pseudocode:
        For LNC2, the result is computed as:

        result = LayerNorm(hidden, gamma, beta, eps)
        result = result.reshape((BxS, H))
        t0 = result[:, 0:H//LNC_SIZE]
        t1 = result[:, H//LNC_SIZE:]
        t0 = t0.reshape((BxS, 128, H//128//LNC_SIZE)).transpose((1, 0, 2))
        t1 = t1.reshape((BxS, 128, H//128//LNC_SIZE)).transpose((1, 0, 2))
        result = np.concatenate([t0, t1], axis=2)
    """

    # Hardware partition dim constraint
    H0 = nl.tile_size.pmax

    # check if input tensor is in sbuf
    input_in_sbuf = input.buffer == nl.sbuf

    # check if output tensor is in sbuf
    output_in_sbuf = output.buffer == nl.sbuf

    """Extract input tensor dimensions where H0=128 (partition dim) and H1=H//128."""
    if input_in_sbuf:
        _H0, BxS, H1 = input.shape
        kernel_assert(
            _H0 == H0,
            f"Input tensor in SBUF does not have partition dimension H0 of 128, got {_H0}",
        )
        H = _H0 * H1
    else:
        B, S, H = input.shape
        BxS = B * S
        kernel_assert(H % H0 == 0, f"Input tensor H dimension must be divisible by {H0}, got {H}")
        H1 = H // H0

    kernel_assert(
        output.shape == (H0, BxS, H1),
        f"Output shape expected is (H0, BxS, H1): {(H0, BxS, H1)}, got {output.shape}",
    )

    # Initialize SBUF manager if not provided
    if not sbm:
        # Calculate required SBUF size: 16*BxS*H1 for intermediates + H0 for constants
        # Factor of 4 accounts for float32 byte size
        sbm = SbufManager(0, (16 * BxS * H1 + H0) * 4, get_logger("layernorm_tkg"), use_auto_alloc=True)

    # Open SBUF memory scope - lv0
    sbm.open_scope(name="layernorm_tkg")

    # Allocate intermediate result buffer in SBUF for computation
    if output_in_sbuf:
        sharded_sbuf_result = output
    else:
        if use_heap_memory:
            sharded_sbuf_result = sbm.alloc_heap(
                (H0, BxS, H1), dtype=input.dtype, buffer=nl.sbuf, name="layernorm_shared_sbuf"
            )
        else:
            sharded_sbuf_result = sbm.alloc_stack(
                (H0, BxS, H1), dtype=input.dtype, buffer=nl.sbuf, name="layernorm_shared_sbuf"
            )

    # Determine sharding configuration for parallel processing
    _, lnc, shard_id = get_verified_program_sharding_info("layernorm_tkg", (0, 1))

    # Apply sharding only if beneficial: LNC2 + sufficient batch size + divisible
    num_shards = lnc
    do_shard = num_shards == 2 and BxS > SHARDING_THRESHOLD and BxS % lnc == 0
    if not do_shard:
        num_shards, shard_id = 1, 0  # Fall back to single core processing

    # Calculate work distribution per shard
    shard_size = BxS // num_shards

    # Execute LayerNorm computation on assigned shard
    layernorm_tkg_llama_impl(
        input=input,
        gamma=gamma,
        beta=beta,
        result=sharded_sbuf_result,
        bs_lb=shard_id * shard_size,
        bs_count=shard_size,
        lnc=lnc,
        eps=eps,
        use_heap_memory=use_heap_memory,
        sbm=sbm,
    )

    # Handle output based on requested buffer location
    if output_in_sbuf:
        # If sharded, exchange results between cores to get complete output
        if do_shard:
            nisa.sendrecv(
                dst=sharded_sbuf_result[:, nl.ds((1 - shard_id) * shard_size, shard_size), :],
                src=sharded_sbuf_result[:, nl.ds(shard_id * shard_size, shard_size), :],
                send_to_rank=1 - shard_id,
                recv_from_rank=1 - shard_id,
                pipe_id=0,
            )

        sbm.close_scope()

        return sharded_sbuf_result.reshape((H0, BxS, H1))

    # Copy results from SBUF to HBM
    sharded_sbuf_result = sharded_sbuf_result.reshape((H0, BxS, H1))
    output = output.reshape(sharded_sbuf_result.shape)

    # Copy only this shard's portion to HBM
    nisa.dma_copy(
        dst=output[:, nl.ds(shard_id * shard_size, shard_size), :],
        src=sharded_sbuf_result[:, nl.ds(shard_id * shard_size, shard_size), :],
    )

    # Cleanup: deallocate sharded_sbuf_result
    if use_heap_memory:
        sbm.pop_heap()

    # Close SBUF memory scope - lv0
    sbm.close_scope()

    return output


def layernorm_tkg_llama_impl(
    input: nl.ndarray,
    gamma: nl.ndarray,
    beta: Optional[nl.ndarray],
    result: nl.ndarray,
    bs_lb: int,
    bs_count: int,
    lnc: int,
    eps: float,
    use_heap_memory: bool,
    sbm: SbufManager,
):
    """
    Perform LayerNorm on a shard of the input tensor.

    The input is of shape [B, S, H]. H0 = nl.tile_size.pmax (128), H1 = H // H0.
    Input is split to [B, S, #lnc, H//#lnc], reshaped to [BxS, #lnc, H0, H1//#lnc],
    transposed to [H0, BxS, #lnc, H1//#lnc], and reshaped back to [H0, BxS, H1].
    LayerNorm is then performed on the combined [H0, #lnc, H1//#lnc] dimension.

    This kernel utilizes Static DMA for input data reads. Experimental results indicate
    that Static DMA offers superior performance. We may revert to DGE in the event of
    HBM out-of-memory (OOM) issues.

    Dimensions:
        B: Batch size
        S: Sequence length
        H: Hidden dimension size
        H0: Partition dimension (128)
        H1: H // H0
        H2: H1 // lnc (sharded partition size)

    Args:
        input (nl.ndarray): [B, S, H] or [H0, BxS, H1] if in SBUF. Tensor to perform LayerNorm on.
            H must be divisible by 128. BxS*(H//128) must fit in SBUF.
        gamma (nl.ndarray): [1, H], Gamma tensor for LayerNorm.
        beta (nl.ndarray): Optional. [1, H], Beta tensor for LayerNorm.
        result (nl.ndarray): [H0, BxS, H1], SBUF tensor to write the result.
        bs_lb (int): Inclusive lower bound of where to start processing on BxS dimension.
        bs_count (int): Number of batches to process on BxS.
        lnc (int): Output sharding layout.
        eps (float): Epsilon to maintain numerical stability.
        use_heap_memory (bool): Indicates whether to allocate memory on the heap instead of the stack.
        sbm (SbufManager): Instance of SbufManager responsible for handling SBUF allocation.

    Returns:
        None. Result is written in-place to the result SBUF tensor.

    Notes:
        - Uses Static DMA for input reads for better performance.
        - All intermediates use FP32 for numerical precision.
        - Variance is computed as Var(X) = E[X^2] - E[X]^2.

    Pseudocode:
        1. Load input from HBM to SBUF with layout transformation
        2. Load gamma (and optionally beta) from HBM to SBUF
        3. Compute mean(x^2) via tensor_reduce + nc_matmul with 1/H
        4. Compute mean(x) via tensor_reduce + nc_matmul with 1/H
        5. Center input: input = input - mean(x)
        6. Compute variance: var = mean(x^2) - mean(x)^2
        7. Compute rsqrt(var + eps)
        8. Normalize: input = input * rsqrt
        9. Apply gamma: input = input * gamma
        10. Optionally apply beta: input = input + beta
    """

    # Hardware partition dim constraint
    H0 = nl.tile_size.pmax

    # check if input tensor is in sbuf
    input_in_sbuf = input.buffer == nl.sbuf

    # Extract input dimensions: Batch, Sequence, Hidden
    if input_in_sbuf:
        _H0, full_BxS, H1 = input.shape
        kernel_assert(
            _H0 == H0,
            f"inp tensor in SBUF does not have partition dimension H0 of {H0}, got {_H0}",
        )
        H = _H0 * H1
    else:
        B, S, H = input.shape
        full_BxS = B * S
        kernel_assert(H % H0 == 0, f"inp tensor H dimension must be divisible by {H0}, got {H}")
        H1 = H // H0

    BxS = bs_count
    H2 = H1 // lnc  # sharded partition size for LNC sharding

    # All intermediates need to happen in FP32 for numerical precision
    inter_dtype = nl.float32

    # Gamma shape check
    kernel_assert(
        gamma.shape == (1, H),
        f"Malformed shape of gamma expected (1, {H}), got {gamma.shape}",
    )

    # Beta check
    is_beta = beta != None

    # Check if the kernel uses auto or manual allocation
    is_auto_alloc = sbm.is_auto_alloc()

    # Define allocation function: heap or stack
    if use_heap_memory:
        alloc_tensor = sbm.alloc_heap
    else:
        alloc_tensor = sbm.alloc_stack

    # Track number of allocated tensors for cleanup
    num_allocated_tensor = 0

    # Open SBUF memory scope - lv1
    sbm.open_scope(name="layernorm_impl_lv1")

    # SBUF tensor for input data
    # reuse result buffer to save memory
    if input_in_sbuf:
        input_sb = input
    else:
        input_sb = result

    # Allocate SBUF tensor for gamma tensor
    gamma_sb = alloc_tensor((H0, H1), dtype=gamma.dtype, buffer=nl.sbuf, name="layernorm_gamma")
    num_allocated_tensor += 1

    # Allocate SBUF tensor for beta tensor
    if is_beta:
        beta_sb = alloc_tensor((H0, H1), dtype=beta.dtype, buffer=nl.sbuf, name="layernorm_beta")
        num_allocated_tensor += 1

    # Load input, gamma and beta tensors from HBM to SBUF
    if not input_in_sbuf:
        # Transform input: (B,S,H) -> (B,S,lnc,H0,H2) -> (BxS,lnc,H0,H2) -> (BxS,lnc,H0,H2) -> (H0,BxS,lnc,H2)
        input_view = (
            TensorView(input)
            .reshape_dim(dim=2, shape=[lnc, H0, H2])
            .flatten_dims(start_dim=0, end_dim=1)
            .slice(dim=0, start=bs_lb, end=bs_lb + BxS)
            .permute(dims=[2, 0, 1, 3])
        )
        # input_sb (H0,BxS,H1) -> (H0,BxS,lnc,H2)
        input_load_view = (
            TensorView(input_sb).reshape_dim(dim=2, shape=[lnc, H2]).slice(dim=1, start=bs_lb, end=bs_lb + BxS)
        )
        nisa.dma_copy(
            dst=input_load_view.get_view(),
            src=input_view.get_view(),
            dge_mode=_DGE_MODE_NONE,
        )

    input_sb_view = TensorView(input_sb).slice(dim=1, start=bs_lb, end=bs_lb + BxS)

    # Transform gamma for sharded layout: (1,H) -> (1,lnc,H0,H2) -> (lnc,H0,H2) -> (H0,lnc,H2)
    gamma_view = TensorView(gamma.reshape((lnc, H0, H2))).permute([1, 0, 2])
    nisa.dma_copy(
        dst=gamma_sb.reshape((H0, lnc, H2)),
        src=gamma_view.get_view(),
        dge_mode=_DGE_MODE_NONE,
    )

    # Transform beta for sharded layout: (1,H) -> (1,lnc,H0,H2) -> (lnc,H0,H2) -> (H0,lnc,H2)
    if is_beta:
        beta_view = TensorView(beta.reshape((lnc, H0, H2))).permute([1, 0, 2])
        nisa.dma_copy(
            dst=beta_sb.reshape((H0, lnc, H2)),
            src=beta_view.get_view(),
            dge_mode=_DGE_MODE_NONE,
        )

    # shared params
    zero_bias = alloc_tensor((H0, 1), dtype=inter_dtype, buffer=nl.sbuf)
    nisa.memset(zero_bias, value=0.0)
    num_allocated_tensor += 1

    # Open SBUF memory scope - lv2
    sbm.open_scope(name="layernorm_impl_lv2")

    reduction_const = alloc_tensor((H0, H0), dtype=inter_dtype, buffer=nl.sbuf)
    nisa.memset(dst=reduction_const, value=(1.0 / H))
    num_allocated_tensor += 1

    # Step 1: Calculate mean(x^2) for variance computation
    # Element-wise squares for RMS calculation
    input_squared_sb = alloc_tensor((H0, BxS, H1), dtype=inter_dtype, buffer=nl.sbuf)
    nisa.activation(
        dst=input_squared_sb[...],
        op=nl.square,
        data=input_sb_view.get_view(),
        bias=zero_bias,
    )
    num_allocated_tensor += 1

    # Reduce squares along H1 dimension to compute mean(x^2)
    reduced_input_squared_sb = alloc_tensor((H0, BxS), dtype=inter_dtype, buffer=nl.sbuf)
    nisa.tensor_reduce(dst=reduced_input_squared_sb[...], op=nl.add, data=input_squared_sb[...], axis=1)
    num_allocated_tensor += 1

    # Complete mean(x^2) calculation using matrix multiplication with 1/H constant
    if is_auto_alloc:
        input_squared_mean = nl.ndarray((H0, BxS), dtype=inter_dtype, buffer=nl.psum)
    else:  # Manual allocation: use PSUM bank 0
        input_squared_mean = nl.ndarray((H0, BxS), dtype=inter_dtype, buffer=nl.psum, address=(0, 0))
    nisa.nc_matmul(
        dst=input_squared_mean,
        stationary=reduction_const,
        moving=reduced_input_squared_sb,
    )

    # Step 2: Calculate mean(x)
    # Sum input values along H1 dimension
    reduced_input_sb = alloc_tensor((H0, BxS), dtype=inter_dtype, buffer=nl.sbuf)
    nisa.tensor_reduce(dst=reduced_input_sb[...], op=nl.add, data=input_sb_view.get_view(), axis=1)
    num_allocated_tensor += 1

    # Complete mean(x) calculation using matrix multiplication with 1/H constant
    if is_auto_alloc:
        input_mean = nl.ndarray((H0, BxS), dtype=inter_dtype, buffer=nl.psum)
    else:  # Manual allocation: use PSUM bank 1
        input_mean = nl.ndarray((H0, BxS), dtype=inter_dtype, buffer=nl.psum, address=(0, 1 * 512 * 4))
    nisa.nc_matmul(dst=input_mean, stationary=reduction_const, moving=reduced_input_sb)

    # Cleanup intermediate tensors if using heap allocation
    if use_heap_memory:
        sbm.pop_heap()  # Deallocate reduced_input_sb
        sbm.pop_heap()  # Deallocate reduced_input_squared_sb
        sbm.pop_heap()  # Deallocate input_squared_sb
        sbm.pop_heap()  # Deallocate reduction_const
        num_allocated_tensor -= 4

    sbm.close_scope()  # Close inner SBUF scope - lv2

    # Step 3: Center the input by subtracting mean
    # Broadcast mean from (H0,BxS) to (H0,BxS,H1) for element-wise subtraction
    input_mean_view = TensorView(input_mean).expand_dim(dim=2).broadcast(dim=2, size=H1)
    nisa.tensor_tensor(
        input_sb_view.get_view(),
        input_sb_view.get_view(),
        input_mean_view.get_view(),
        nl.subtract,
    )

    # Step 4: Calculate variance using Var(X) = E[X^2] - E[X]^2
    # Square E(x) values
    squared_input_mean = alloc_tensor((H0, BxS), dtype=inter_dtype, buffer=nl.sbuf)
    nisa.activation(dst=squared_input_mean[...], op=nl.square, data=input_mean[...], bias=zero_bias)
    num_allocated_tensor += 1

    # Compute variance: E[X^2] - E[X]^2
    var = alloc_tensor((H0, BxS), dtype=inter_dtype, buffer=nl.sbuf)
    nisa.tensor_tensor(var[...], input_squared_mean[...], squared_input_mean[...], nl.subtract)
    num_allocated_tensor += 1

    # Step 5: Compute normalization factor 1/sqrt(var + eps)
    rsqrt = alloc_tensor((H0, BxS), dtype=inter_dtype, buffer=nl.sbuf)
    eps_bias = alloc_tensor((H0, 1), dtype=inter_dtype, buffer=nl.sbuf)
    nisa.memset(eps_bias, value=eps)
    nisa.activation(dst=rsqrt[...], op=nl.rsqrt, data=var[...], bias=eps_bias)
    num_allocated_tensor += 2

    # Step 6: Normalize the centered input: (input - mean) / sqrt(var + eps)
    # Broadcast normalization factor from (H0,BxS) to (H0,BxS,H1)
    rsqrt_view = TensorView(rsqrt).expand_dim(dim=2).broadcast(dim=2, size=H1)
    nisa.tensor_tensor(
        input_sb_view.get_view(),
        input_sb_view.get_view(),
        rsqrt_view.get_view(),
        nl.multiply,
    )

    # Step 7: Apply gamma scaling
    # Broadcast gamma from (H0,H1) to (H0,BxS,H1) for element-wise multiplication
    gamma_sb_view = TensorView(gamma_sb).expand_dim(dim=1).broadcast(dim=1, size=BxS)
    nisa.tensor_tensor(
        input_sb_view.get_view(),
        input_sb_view.get_view(),
        gamma_sb_view.get_view(),
        nl.multiply,
    )

    # Step 8: Apply beta bias if present
    if is_beta:
        # Broadcast beta from (H0,H1) to (H0,BxS,H1) for element-wise addition
        beta_sb_view = TensorView(beta_sb).expand_dim(dim=1).broadcast(dim=1, size=BxS)
        nisa.tensor_tensor(
            input_sb_view.get_view(),
            input_sb_view.get_view(),
            beta_sb_view.get_view(),
            nl.add,
        )

    # Copy final result to output buffer
    if input_in_sbuf:
        nisa.tensor_copy(dst=result[0:H0, nl.ds(bs_lb, bs_count), 0:H1], src=input_sb_view.get_view())

    # Cleanup: deallocate heap memory if used
    if use_heap_memory:
        for _ in range(num_allocated_tensor):
            sbm.pop_heap()

    # Close SBUF memory scope - lv1
    sbm.close_scope()
