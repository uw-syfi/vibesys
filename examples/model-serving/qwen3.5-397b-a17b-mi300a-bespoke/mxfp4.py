"""OCP MXFP4 dequantization in plain torch.

A weight of logical shape [..., K] is stored as uint8 [..., K/2] (two fp4 e2m1
values per byte, low nibble = even element, verified against the FP8 source
checkpoint, see reference/README.md) plus uint8 e8m0 scales [..., K/32].
"""

import torch

BLOCK = 32
# fp4 e2m1: bit 3 is the sign, bits 0-2 index the magnitudes below.
_FP4_VALUES = (
    0.0,
    0.5,
    1.0,
    1.5,
    2.0,
    3.0,
    4.0,
    6.0,
    -0.0,
    -0.5,
    -1.0,
    -1.5,
    -2.0,
    -3.0,
    -4.0,
    -6.0,
)


def dequant_mxfp4(packed: torch.Tensor, scale: torch.Tensor, dtype: torch.dtype) -> torch.Tensor:
    """Return the dense [..., K] tensor for packed [..., K/2] and scale [..., K/32]."""
    lut = torch.tensor(_FP4_VALUES, dtype=torch.float32, device=packed.device)
    lo = lut[(packed & 0xF).long()]
    hi = lut[(packed >> 4).long()]
    values = torch.stack([lo, hi], dim=-1).flatten(-2)  # interleave: lo, hi, lo, hi, ...
    scales = torch.exp2(scale.float() - 127.0).repeat_interleave(
        BLOCK, dim=-1
    )  # e8m0 (255 = NaN, ignored)
    return (values * scales).to(dtype)
