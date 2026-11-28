"""Serving precision primitives shared by the synthetic model oracles.

Model activations are BF16. Normalization and attention reductions use FP32,
and their results return to the activation dtype at operator boundaries.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F


def rms(x: torch.Tensor, weight: torch.Tensor, eps: float = 1e-6,
        *, gemma: bool = False, weight_in_fp32: bool = False) -> torch.Tensor:
    scale = 1.0 + weight.float() if gemma else weight.float()
    normalized = x.float() * torch.rsqrt(x.float().square().mean(-1, keepdim=True) + eps)
    # Gemma applies its offset weight before the activation cast; ordinary
    # Llama/Qwen RMSNorm casts the normalized activation before weighting.
    if gemma or weight_in_fp32:
        return (normalized * scale).to(x.dtype)
    return normalized.to(x.dtype) * weight.to(x.dtype)


def attention(query: torch.Tensor, key: torch.Tensor, value: torch.Tensor,
              *, scale: float | None = None,
              allowed: torch.Tensor | None = None) -> torch.Tensor:
    """GQA over [tokens, heads, dim], or one [heads, dim] decode query.

    The eager serving path publishes BF16 scores/probabilities, with FP32
    softmax reduction. A boolean mask uses True for visible positions.
"""
    single = query.ndim == 2
    if single:
        query = query.unsqueeze(0)
    groups = query.shape[-2] // key.shape[-2]
    key = key.repeat_interleave(groups, dim=-2)
    value = value.repeat_interleave(groups, dim=-2)
    q, k, v = (tensor.transpose(0, 1) for tensor in (query, key, value))
    scores = (q @ k.transpose(-1, -2)) * (scale if scale is not None else query.shape[-1] ** -0.5)
    if allowed is not None:
        scores = scores.masked_fill(~allowed, -torch.inf)
    probabilities = F.softmax(scores, dim=-1, dtype=torch.float32).to(query.dtype)
    result = (probabilities @ v).transpose(0, 1)
    return result.squeeze(0) if single else result


def rope(x: torch.Tensor, positions: torch.Tensor, theta: float,
         *, factor: float = 1.0) -> torch.Tensor:
    """Rotate half the head dimensions, with Gemma's optional position scale."""
    width = x.shape[-1]
    inv = theta ** (-torch.arange(0, width, 2, device=x.device,
                                  dtype=torch.float64) / width)
    phase = torch.outer(positions.to(torch.float64) / factor, inv)
    phase = torch.cat((phase, phase), dim=-1)
    cos, sin = phase.cos().float(), phase.sin().float()
    while cos.ndim < x.ndim:
        cos, sin = cos.unsqueeze(-2), sin.unsqueeze(-2)
    half = width // 2
    rotated = torch.cat((-x[..., half:], x[..., :half]), dim=-1)
    return x * cos.to(x.dtype) + rotated * sin.to(x.dtype)


def llama31_rope(x: torch.Tensor, positions: torch.Tensor) -> torch.Tensor:
    """Llama 3.1's wavelength-dependent factor-8 extension, original size 8192."""
    width = x.shape[-1]
    inv = 500_000.0 ** (-torch.arange(0, width, 2, device=x.device,
                                    dtype=torch.float64) / width)
    wavelength = 2 * torch.pi / inv
    smooth = (8192 / wavelength - 1) / 3
    blended = (1 - smooth) * inv / 8 + smooth * inv
    inv = torch.where(wavelength > 8192, inv / 8,
                      torch.where(wavelength < 2048, inv, blended))
    phase = torch.outer(positions.to(torch.float64), inv)
    phase = torch.cat((phase, phase), -1)
    cos, sin = phase.cos().float(), phase.sin().float()
    while cos.ndim < x.ndim:
        cos, sin = cos.unsqueeze(-2), sin.unsqueeze(-2)
    half = width // 2
    rotated = torch.cat((-x[..., half:], x[..., :half]), -1)
    return x * cos.to(x.dtype) + rotated * sin.to(x.dtype)


def random_weight(generator: torch.Generator, device: str, shape: tuple[int, ...],
                  fan_in: int) -> torch.Tensor:
    return (torch.randn(shape, generator=generator, device=device) *
            fan_in ** -0.5).to(torch.bfloat16)


def random_norm(generator: torch.Generator, device: str, shape: tuple[int, ...],
                *, gemma: bool = False) -> torch.Tensor:
    values = 0.1 * torch.randn(shape, generator=generator, device=device)
    return (values if gemma else 1.0 + values).to(torch.bfloat16)


def fill_layer_weights(generator: torch.Generator, device: str,
                       shape: tuple[int, ...], fan_in: int) -> torch.Tensor:
    """Avoid a full-stack fp32 temporary for model-sized BF16 weights."""
    out = torch.empty(shape, device=device, dtype=torch.bfloat16)
    for layer in range(shape[0]):
        out[layer].copy_(random_weight(generator, device, shape[1:], fan_in))
    return out
