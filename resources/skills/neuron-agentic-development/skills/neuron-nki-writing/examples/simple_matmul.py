# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Simple matrix multiplication kernel for NKI.

Demonstrates PSUM usage and matrix multiply with tiling.
Based on the NKI matrix multiplication tutorial's basic pattern.
"""

import nki
import nki.isa as nisa
import nki.language as nl


# === Self-contained utilities ===

def kernel_assert(condition: bool, error_text: str):
    assert condition, f"[INTERNAL_ERROR] [NCC_INKI016] Kernel validation exception: {error_text}"

def div_ceil(n: int, d: int) -> int:
    return (n + d - 1) // d


# === Hardware constants for nc_matmul ===
# nc_matmul: dst = stationary.T @ moving
#   stationary: [K, M] - K is partition (contraction), M is stationary free
#   moving: [K, N] - K is partition, N is moving free
#   dst: [M, N] in PSUM
#
# For lhsT[K,M] @ rhs[K,N] = output[M,N]:
#   The caller must transpose the left operand before passing it in.
P_MAX = 128        # Partition dimension max (K for matmul)
F_STAT_MAX = 128   # Stationary free dimension max (M for matmul)
F_MOV_MAX = 512    # Moving free dimension max (N for matmul)


@nki.jit
def simple_matmul(
    lhs_T: nl.ndarray,
    rhs: nl.ndarray,
) -> nl.ndarray:
    """
    Compute matrix multiplication with pre-transposed left operand.

    This kernel implements a tiled matrix multiplication following the
    pattern from the NKI matrix multiplication tutorial. The caller must
    transpose the left operand before calling this kernel.

    Computation: output[M, N] = lhs_T.T @ rhs = lhs @ rhs

    nc_matmul semantics: dst = stationary.T @ moving
        - stationary: [K, M] - K is partition (contraction), M is stationary free
        - moving: [K, N] - K is partition (contraction), N is moving free
        - dst: [M, N] in PSUM

    Dimensions:
        K: Contraction dimension (partition dimension, tiled by P_MAX=128)
        M: Rows of output (stationary free dimension, tiled by F_STAT_MAX=128)
        N: Columns of output (moving free dimension, tiled by F_MOV_MAX=512)

    Args:
        lhs_T (nl.ndarray): [K, M] @ HBM, left operand TRANSPOSED
                           (if original lhs is [M, K], pass lhs.T)
        rhs (nl.ndarray): [K, N] @ HBM, right operand

    Returns:
        nl.ndarray: [M, N] @ HBM, result matrix output = lhs_T.T @ rhs

    Notes:
        - K (partition) dimension limited to 128 per tile
        - M (stationary free) dimension limited to 128 per tile
        - N (moving free) dimension limited to 512 per tile
        - Uses float32 accumulation in PSUM
        - No nc_transpose needed - caller provides pre-transposed data

    Performance:
        This kernel uses the proper PSUM accumulation pattern for efficient matrix
        multiplication. The K-dimension loop uses nl.affine_range (NOT nl.sequential_range)
        to allow the compiler to detect multiple writes to the same PSUM buffer and trigger
        hardware PSUM accumulation. Using nl.sequential_range would serialize execution and
        prevent efficient PSUM accumulation, causing severe performance degradation.

    Example:
        # To compute C = A @ B where A is [M, K] and B is [K, N]:
        C = simple_matmul(A.T, B)  # Pass A transposed

    Pseudocode:
        output = zeros(M, N)
        for m_tile in tiles(M, F_STAT_MAX):
            for n_tile in tiles(N, F_MOV_MAX):
                accum = zeros(m_tile.size, n_tile.size)  # in PSUM
                for k_tile in sequential_tiles(K, P_MAX):
                    lhs_T_tile = load(lhs_T[k_tile, m_tile])  # [K, M]
                    rhs_tile = load(rhs[k_tile, n_tile])      # [K, N]
                    # nc_matmul: accum += lhs_T_tile.T @ rhs_tile
                    accum += matmul(stationary=lhs_T_tile, moving=rhs_tile)
                store(output[m_tile, n_tile], accum)
        return output
    """
    # === Validate Input ===
    kernel_assert(len(lhs_T.shape) == 2, f"lhs_T must be 2D, got {len(lhs_T.shape)}D")
    kernel_assert(len(rhs.shape) == 2, f"rhs must be 2D, got {len(rhs.shape)}D")

    K, M = lhs_T.shape
    K_rhs, N = rhs.shape

    kernel_assert(
        K == K_rhs,
        f"Contraction dimension mismatch: lhs_T has K={K}, rhs has K={K_rhs}"
    )

    # === Allocate Output ===
    output = nl.ndarray((M, N), dtype=lhs_T.dtype, buffer=nl.shared_hbm)

    # === Calculate Tiling ===
    # K is partition dimension (tiled by P_MAX=128)
    # M is stationary free dimension (tiled by F_STAT_MAX=128)
    # N is moving free dimension (tiled by F_MOV_MAX=512)
    num_m_tiles = div_ceil(M, F_STAT_MAX)
    num_n_tiles = div_ceil(N, F_MOV_MAX)
    num_k_tiles = div_ceil(K, P_MAX)

    # === Main Processing Loop ===
    # Tile M by stationary free dimension limit
    for m_idx in nl.affine_range(num_m_tiles):
        m_start = m_idx * F_STAT_MAX
        m_end = min(m_start + F_STAT_MAX, M)
        m_size = m_end - m_start

        # Tile N by moving free dimension limit
        for n_idx in nl.affine_range(num_n_tiles):
            n_start = n_idx * F_MOV_MAX
            n_end = min(n_start + F_MOV_MAX, N)
            n_size = n_end - n_start

            # Initialize accumulator in PSUM
            # PSUM shape: [M_tile, N_tile]
            accum_psum = nl.ndarray((F_STAT_MAX, F_MOV_MAX), dtype=nl.float32, buffer=nl.psum)

            # Loop over K tiles for PSUM accumulation.
            # CRITICAL: Use affine_range (not sequential_range) to allow the
            # compiler to detect multiple writes to the same PSUM buffer and
            # trigger efficient hardware PSUM accumulation. Using sequential_range
            # would serialize execution and prevent proper PSUM accumulation.
            for k_idx in nl.affine_range(num_k_tiles):
                k_start = k_idx * P_MAX
                k_end = min(k_start + P_MAX, K)
                k_size = k_end - k_start

                # --- Load lhs_T tile for stationary operand ---
                # lhs_T is [K, M], shape [P, F_stat] = [K_tile, M_tile]
                lhs_T_sb = nl.ndarray((P_MAX, F_STAT_MAX), dtype=lhs_T.dtype, buffer=nl.sbuf)
                nisa.dma_copy(
                    dst=lhs_T_sb[0:k_size, 0:m_size],
                    src=lhs_T[k_start:k_end, m_start:m_end],
                )

                # --- Load rhs tile for moving operand ---
                # rhs is [K, N], shape [P, F_mov] = [K_tile, N_tile]
                rhs_sb = nl.ndarray((P_MAX, F_MOV_MAX), dtype=rhs.dtype, buffer=nl.sbuf)
                nisa.dma_copy(
                    dst=rhs_sb[0:k_size, 0:n_size],
                    src=rhs[k_start:k_end, n_start:n_end],
                )

                # --- Matrix multiply and accumulate ---
                # nc_matmul: dst = stationary.T @ moving
                # stationary = lhs_T[K, M], moving = rhs[K, N]
                # dst = lhs_T.T @ rhs = lhs @ rhs = [M, N]
                nisa.nc_matmul(
                    dst=accum_psum[0:m_size, 0:n_size],
                    stationary=lhs_T_sb[0:k_size, 0:m_size],
                    moving=rhs_sb[0:k_size, 0:n_size],
                )

            # --- Copy from PSUM to SBUF, convert to output dtype ---
            result_sb = nl.ndarray((F_STAT_MAX, F_MOV_MAX), dtype=output.dtype, buffer=nl.sbuf)
            nisa.tensor_copy(
                dst=result_sb[0:m_size, 0:n_size],
                src=accum_psum[0:m_size, 0:n_size],
            )

            # --- Store to HBM ---
            nisa.dma_copy(
                dst=output[m_start:m_end, n_start:n_end],
                src=result_sb[0:m_size, 0:n_size],
            )

    return output


if __name__ == "__main__":
    """
    Test script for simple_matmul kernel.

    This test validates correctness against PyTorch reference implementation.
    """
    import os
    import torch
    from torch_xla.core import xla_model as xm

    # Set environment variables for profiling/debugging
    os.environ['NEURON_RT_INSPECT_ENABLE'] = '1'
    os.environ['NEURON_RT_INSPECT_DEVICE_PROFILE'] = '1'
    os.environ["NEURON_RT_INSPECT_OUTPUT_DIR"] = "./output_simple_matmul"
    os.environ["NEURON_CC_FLAGS"] = "--target trn2"
    os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"

    # Setup the XLA device and generate input tensors
    device = xm.xla_device()

    # Test with matrices to exercise tiling
    # A[M, K] @ B[K, N] = C[M, N]
    # Using dimensions that exercise tiling:
    # M=128: Exactly at F_STAT_MAX (no tiling needed in stationary free)
    # K=128: Exactly at P_MAX (no tiling needed in partition)
    # N=1024: Exercises moving free dimension tiling (F_MOV_MAX=512) → 2 tiles
    M, K, N = 128, 128, 1024
    a = torch.randn((M, K), dtype=torch.float16).to(device=device)
    b = torch.randn((K, N), dtype=torch.float16).to(device=device)

    # Compute reference output with PyTorch
    expected = torch.matmul(a, b)

    # Invoke the kernel with transposed left operand
    # simple_matmul expects lhs_T[K, M] and rhs[K, N]
    c = simple_matmul(a.T, b)

    # Move results to CPU for comparison
    c_cpu = c.cpu()
    expected_cpu = expected.cpu()

    # Compute absolute difference
    abs_diff = torch.abs(c_cpu - expected_cpu)
    max_abs_diff = torch.max(abs_diff).item()

    # For float16 matmul, expect tolerance around 1e-2
    tolerance = 1e-2

    print("=" * 60)
    print("simple_matmul Test Results")
    print("=" * 60)
    print(f"A shape: {a.shape}")
    print(f"B shape: {b.shape}")
    print(f"C shape: {c.shape}")
    print(f"Max absolute difference: {max_abs_diff:.6f}")
    print(f"Tolerance: {tolerance}")

    if max_abs_diff < tolerance:
        print("✓ Test PASSED - Output matches PyTorch matmul within tolerance")
    else:
        print("✗ Test FAILED - Output differs from PyTorch matmul")
        print(f"NKI output sample:   {c_cpu[0, :5]}")
        print(f"Expected sample:     {expected_cpu[0, :5]}")
        print(f"Difference sample:   {abs_diff[0, :5]}")
        exit(1)

    print("=" * 60)
