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

"""PyTorch reference implementations for the rmsnorm_tkg kernel."""

from typing import Optional

import torch


def rms_norm_torch_ref(
    hidden: torch.Tensor,
    gamma: Optional[torch.Tensor],
    eps: float = 1e-6,
    hidden_actual: Optional[int] = None,
    **_,
) -> torch.Tensor:
    """
    PyTorch reference implementation of RMS normalization.

    Args:
        hidden: Input tensor to normalize.
        gamma: Scale parameter (optional).
        eps: Epsilon for numerical stability.
        hidden_actual: Actual hidden dimension for padded inputs.

    Returns:
        Normalized tensor.
    """
    # All intermediates need to happen in FP32 for numerical precision
    hidden = hidden.to(torch.float32)

    if hidden_actual is not None:
        sum_squares = hidden.square().sum(dim=-1, keepdim=True)
        rms = (sum_squares / hidden_actual + eps).sqrt()
    else:
        rms = (hidden.square().mean(dim=-1, keepdim=True) + eps).sqrt()

    norm = hidden * rms.reciprocal()
    if gamma is not None:
        norm *= gamma
    return norm


def rmsnorm_tkg_torch_ref_lnc1(
    input: torch.Tensor,
    gamma: torch.Tensor,
    output: torch.Tensor,
    eps: float = 1e-6,
    hidden_actual: Optional[int] = None,
    hidden_dim_tp: bool = False,
    single_core_forced: bool = False,
    use_heap_memory: bool = False,
    sbm: Optional[object] = None,
) -> dict[str, torch.Tensor]:
    """Torch reference for rmsnorm_tkg kernel (LNC1 output layout).

    This is a reference implementation for testing the NKI rmsnorm_tkg kernel.
    It applies RMS normalization and reshapes the output into the LNC1 tile layout.

    Args:
        input (torch.Tensor): [B, S, H] input hidden states.
        gamma (torch.Tensor): [1, H] RMS norm weight vector.
        output (torch.Tensor): [128, B*S, H//128] output buffer. Unused, present for interface compatibility.
        eps (float): Epsilon for numerical stability.
        hidden_actual (int or None): Actual hidden dim size if padded.
        hidden_dim_tp (bool): If True, use TP-sharded hidden dim layout.
        single_core_forced (bool): Unused, present for interface compatibility.
        use_heap_memory (bool): Unused, present for interface compatibility.
        sbm: Unused, present for interface compatibility.

    Returns:
        dict: {"out": torch.Tensor} with shape [128, B*S, H//128].

    Note:
        Hardware-specific parameters (single_core_forced, use_heap_memory, sbm) are
        accepted but ignored as they don't affect the mathematical result.
    """
    B, S, H = input.shape
    BxS = B * S
    H0, H1 = 128, H // 128
    dtype = input.dtype

    result = rms_norm_torch_ref(input, gamma, eps=eps, hidden_actual=hidden_actual)
    result = result.reshape(BxS, -1)

    if hidden_dim_tp:
        result = result.reshape(BxS, H1, H0).permute(2, 0, 1)
    else:
        result = result.reshape(BxS, H0, H1).permute(1, 0, 2)

    return {"out": result.to(dtype)}


def rmsnorm_tkg_torch_ref(
    input: torch.Tensor,
    gamma: torch.Tensor,
    output: torch.Tensor,
    eps: float = 1e-6,
    hidden_actual: Optional[int] = None,
    hidden_dim_tp: bool = False,
    single_core_forced: bool = False,
    use_heap_memory: bool = False,
    sbm: Optional[object] = None,
) -> dict[str, torch.Tensor]:
    """Torch reference for rmsnorm_tkg kernel (LNC2 output layout).

    This is a reference implementation for testing the NKI rmsnorm_tkg kernel.
    It applies RMS normalization and reshapes the output into the LNC2 tile layout,
    which interleaves two halves of the hidden dimension.

    Args:
        input (torch.Tensor): [B, S, H] input hidden states.
        gamma (torch.Tensor): [1, H] RMS norm weight vector.
        output (torch.Tensor): [128, B*S, H//128] output buffer. Unused, present for interface compatibility.
        eps (float): Epsilon for numerical stability.
        hidden_actual (int or None): Actual hidden dim size if padded.
        hidden_dim_tp (bool): If True, use TP-sharded hidden dim layout.
        single_core_forced (bool): Unused, present for interface compatibility.
        use_heap_memory (bool): Unused, present for interface compatibility.
        sbm: Unused, present for interface compatibility.

    Returns:
        dict: {"out": torch.Tensor} with shape [128, B*S, H//128].

    Note:
        Hardware-specific parameters (single_core_forced, use_heap_memory, sbm) are
        accepted but ignored as they don't affect the mathematical result.
    """
    B, S, H = input.shape
    BxS = B * S
    H0, H1 = 128, H // 128
    dtype = input.dtype

    result = rms_norm_torch_ref(input, gamma, eps=eps, hidden_actual=hidden_actual)
    result = result.reshape(BxS, -1)

    if hidden_dim_tp:
        result = result.reshape(BxS, H1, H0).permute(2, 0, 1)
    else:
        # LNC2: interleave two halves
        t0 = result[:, 0 : H // 2]
        t1 = result[:, H // 2 :]
        t0 = t0.reshape(BxS, H0, H1 // 2).permute(1, 0, 2)
        t1 = t1.reshape(BxS, H0, H1 // 2).permute(1, 0, 2)
        result = torch.cat([t0, t1], dim=2)

    return {"out": result.to(dtype)}
