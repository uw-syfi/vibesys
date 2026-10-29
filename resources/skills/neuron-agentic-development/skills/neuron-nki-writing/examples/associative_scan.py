import nki
import nki.isa as nisa
import nki.language as nl

def kernel_assert(condition, message=""):
    """Validate kernel preconditions (raises at trace time)."""
    assert condition, message


@nki.jit
def cumulative_product_sum(deltaA, deltaBu):
    """Associative scan: out[i] = deltaA[i] * out[i-1] + deltaBu[i]

    This pattern is used in State Space Models (Mamba), RNNs, and cumulative
    operations. Computes sequential operations efficiently in a single instruction.

    Args:
        deltaA: (channels, seq_len) - multiplication factors
        deltaBu: (channels, seq_len) - additive terms

    Returns:
        scan_result: (channels, seq_len) - cumulative scan results

    Notes:
        - Uses nisa.tensor_tensor_scan for efficient sequential operations
        - Handles loop-carried dependencies internally in VectorE
        - channels must be ≤ 128 (partition dimension constraint)
        - Much faster than explicit sequential loops
    """
    channels, seq_len = deltaA.shape
    kernel_assert(channels <= 128, "channels must be ≤ 128")

    output = nl.ndarray((channels, seq_len), dtype=deltaA.dtype, buffer=nl.shared_hbm)

    # Allocate SBUF tiles
    deltaA_tile = nl.ndarray((channels, seq_len), dtype=deltaA.dtype, buffer=nl.sbuf)
    deltaBu_tile = nl.ndarray((channels, seq_len), dtype=deltaBu.dtype, buffer=nl.sbuf)

    # Load from HBM
    nisa.dma_copy(dst=deltaA_tile, src=deltaA[0:channels, 0:seq_len])
    nisa.dma_copy(dst=deltaBu_tile, src=deltaBu[0:channels, 0:seq_len])

    # Associative scan: out[i] = deltaA[i] * out[i-1] + deltaBu[i]
    # initial=0 means out[0] = deltaA[0] * 0 + deltaBu[0] = deltaBu[0]
    scan_result = nl.ndarray((channels, seq_len), dtype=deltaA.dtype, buffer=nl.sbuf)
    nisa.tensor_tensor_scan(
        dst=scan_result,
        data0=deltaA_tile,
        data1=deltaBu_tile,
        initial=0,
        op0=nl.multiply,
        op1=nl.add
    )

    # Store to HBM
    nisa.dma_copy(dst=output, src=scan_result)

    return output

# Simple test function
def test_associative_scan():
    """Test associative scan against PyTorch reference."""
    import torch
    channels, seq_len = 128, 256

    deltaA = torch.ones(channels, seq_len) * 0.9
    deltaBu = torch.ones(channels, seq_len) * 0.1

    # PyTorch reference
    result_torch = torch.zeros(channels, seq_len)
    for i in range(seq_len):
        prev = result_torch[:, i-1] if i > 0 else 0
        result_torch[:, i] = deltaA[:, i] * prev + deltaBu[:, i]

    # NKI kernel
    result_nki = cumulative_product_sum(deltaA, deltaBu)

    # Compare
    assert torch.allclose(result_torch, result_nki, rtol=1e-3)
    print("✓ Associative scan test passed")

if __name__ == "__main__":
    test_associative_scan()
