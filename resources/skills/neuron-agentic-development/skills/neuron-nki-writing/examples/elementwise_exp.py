# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Element-wise exponential kernel for NKI.

Demonstrates the basic NKI kernel pattern: load, compute, store.
"""

import nki
import nki.isa as nisa
import nki.language as nl


# === Self-contained utilities ===

def kernel_assert(condition: bool, error_text: str):
    assert condition, f"[INTERNAL_ERROR] [NCC_INKI016] Kernel validation exception: {error_text}"

def div_ceil(n: int, d: int) -> int:
    return (n + d - 1) // d


# === Hardware constants ===
P_MAX = 128
F_TILE_SIZE = 2048


@nki.jit
def elementwise_exp(x: nl.ndarray) -> nl.ndarray:
    """
    Compute element-wise exponential: y = exp(x).

    This kernel demonstrates the fundamental NKI pattern for element-wise
    operations with tiling for large tensors.

    Dimensions:
        outer_dim: Product of all dimensions except last (collapsed for tiling)
        last_dim: Last dimension of input tensor

    Args:
        x (nl.ndarray): Input HBM tensor of any shape. Last dimension processed
            as free dimension, other dimensions collapsed to partition dimension.

    Returns:
        nl.ndarray: Output HBM tensor with same shape as input, containing exp(x).

    Notes:
        - Supports arbitrary input shapes via reshape to 2D
        - Uses float32 computation internally for numerical stability

    Pseudocode:
        x_2d = x.reshape(outer_dim, last_dim)
        y_2d = zeros_like(x_2d)
        for p_tile in tiles(outer_dim, P_MAX):
            for f_tile in tiles(last_dim, F_TILE_SIZE):
                tile = load(x_2d[p_tile, f_tile])
                result = exp(tile)
                store(y_2d[p_tile, f_tile], result)
        return y_2d.reshape(x.shape)
    """
    # === Reshape to 2D for simpler tiling ===
    x_shape = x.shape
    rank = len(x_shape)

    if rank == 1:
        outer_dim = 1
        last_dim = x_shape[0]
    else:
        outer_dim = 1
        for dim in x_shape[:-1]:
            outer_dim *= dim
        last_dim = x_shape[-1]

    shape_2d = (outer_dim, last_dim)
    x_2d = x.reshape(shape_2d)

    # === Allocate Output ===
    y = nl.ndarray(x_shape, dtype=x.dtype, buffer=nl.shared_hbm)
    y_2d = y.reshape(shape_2d)

    # === Calculate Tiling ===
    num_p_tiles = div_ceil(outer_dim, P_MAX)
    num_f_tiles = div_ceil(last_dim, F_TILE_SIZE)

    # === Main Processing Loop ===
    for p_idx in nl.affine_range(num_p_tiles):
        p_start = p_idx * P_MAX
        p_end = min(p_start + P_MAX, outer_dim)
        p_size = p_end - p_start

        for f_idx in nl.affine_range(num_f_tiles):
            f_start = f_idx * F_TILE_SIZE
            f_end = min(f_start + F_TILE_SIZE, last_dim)
            f_size = f_end - f_start

            # --- Load Input Tile ---
            x_sb = nl.ndarray((P_MAX, F_TILE_SIZE), dtype=x.dtype, buffer=nl.sbuf)
            nisa.dma_copy(
                dst=x_sb[0:p_size, 0:f_size],
                src=x_2d[p_start:p_end, f_start:f_end],
            )

            # --- Compute exp ---
            y_sb = nl.ndarray((P_MAX, F_TILE_SIZE), dtype=x.dtype, buffer=nl.sbuf)
            nisa.activation(
                dst=y_sb[0:p_size, 0:f_size],
                data=x_sb[0:p_size, 0:f_size],
                op=nl.exp,
            )

            # --- Store Output Tile ---
            nisa.dma_copy(
                dst=y_2d[p_start:p_end, f_start:f_end],
                src=y_sb[0:p_size, 0:f_size],
            )

    return y


if __name__ == "__main__":
    """
    Test script for elementwise_exp kernel.

    This test validates correctness against PyTorch reference implementation.
    """
    import os
    import torch
    from torch_xla.core import xla_model as xm

    # Set environment variables for profiling/debugging
    os.environ['NEURON_RT_INSPECT_ENABLE'] = '1'
    os.environ['NEURON_RT_INSPECT_DEVICE_PROFILE'] = '1'
    os.environ["NEURON_RT_INSPECT_OUTPUT_DIR"] = "./output_elementwise_exp"
    os.environ["NEURON_CC_FLAGS"] = "--target trn2"
    os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"

    # Setup the XLA device and generate input tensors
    device = xm.xla_device()

    # Test with larger tensor to exercise tiling logic
    # Shape: (256, 4096) - requires P and F tiling
    # P_MAX=128, so 256 requires 2 partition tiles
    # F_TILE_SIZE=2048, so 4096 requires 2 free dimension tiles
    x = torch.randn((256, 4096), dtype=torch.float16).to(device=device)

    # Compute reference output with PyTorch
    expected = torch.exp(x)

    # Invoke the kernel
    y = elementwise_exp(x)

    # Move results to CPU for comparison
    y_cpu = y.cpu()
    expected_cpu = expected.cpu()
    x_cpu = x.cpu()

    # Compute absolute difference
    abs_diff = torch.abs(y_cpu - expected_cpu)
    max_abs_diff = torch.max(abs_diff).item()

    # For float16, expect tolerance around 1e-2
    tolerance = 1e-2

    print("=" * 60)
    print("elementwise_exp Test Results")
    print("=" * 60)
    print(f"Input shape: {x.shape}")
    print(f"Output shape: {y.shape}")
    print(f"Max absolute difference: {max_abs_diff:.6f}")
    print(f"Tolerance: {tolerance}")

    if max_abs_diff < tolerance:
        print("✓ Test PASSED - Output matches PyTorch exp within tolerance")
    else:
        print("✗ Test FAILED - Output differs from PyTorch exp")
        print(f"Sample input: {x_cpu[0, :5]}")
        print(f"NKI output:   {y_cpu[0, :5]}")
        print(f"Expected:     {expected_cpu[0, :5]}")
        print(f"Difference:   {abs_diff[0, :5]}")
        exit(1)

    print("=" * 60)
