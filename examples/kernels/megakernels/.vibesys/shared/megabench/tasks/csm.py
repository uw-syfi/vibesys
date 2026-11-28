"""Sesame CSM-1B: one audio frame (32 Mimi codebooks) at batch one.

The timed step follows ``CsmGenerationMixin._sample`` in transformers 5.17.0
for one greedy frame:

1. The previous frame's 32 codes are embedded as the sum of the tied audio
   embedding rows ``code[k] + k * audio_vocab``. Generated frames have no
   text slot (``CsmBackboneModelEmbeddings``).
2. One Llama-3.2-style backbone decode step at position ``context`` writes
   one K/V row per layer. ``lm_head`` on the final-norm hidden state gives
   the codebook-0 logits.
3. The depth decoder starts with no cache. Position 0 holds the backbone's
   final-norm hidden state; position ``p`` in 1..31 holds the embedding of
   code ``p - 1`` at offset ``(p - 1) * audio_vocab``. A bias-free projector
   maps every position to the depth width. Position ``p`` predicts code ``p``
   through its own ``codebooks_head`` matrix ``[depth_hidden, audio_vocab]``.

``forced_codes`` replaces the greedy history with given codes, so the logits
of every codebook are conditioned on those codes (teacher forcing). The
grader uses it to regrade near-tie departures. Weights are seeded synthetic
BF16 at full model geometry; activations are BF16 with FP32 normalization and
softmax, as in the eager serving path.
"""

from __future__ import annotations

import math

import torch
import torch.nn.functional as F

from ..cases import Case
from .common import attention, fill_layer_weights, random_norm, random_weight, rms

# Pinned sesame/csm-1b config: Llama-3 RoPE for both transformers.
ROPE_THETA = 500_000.0
BACKBONE_ROPE = {"factor": 32.0, "low_freq_factor": 0.125, "high_freq_factor": 0.5,
                 "original_max_position_embeddings": 1024}
DEPTH_ROPE = {"factor": 32.0, "low_freq_factor": 0.001953125,
              "high_freq_factor": 0.0078125, "original_max_position_embeddings": 16}
RMS_EPS = 1e-5

LAYER_NAMES = ("ln1", "wq", "wk", "wv", "wo", "ln2", "wg", "wu", "wd")


def llama3_inv_freq(width: int, scaling: dict) -> torch.Tensor:
    """``_compute_llama3_parameters`` in FP32 on the host, as HF initializes it."""
    inv = 1.0 / (ROPE_THETA ** (torch.arange(0, width, 2, dtype=torch.int64).float() / width))
    factor, low, high = (scaling["factor"], scaling["low_freq_factor"],
                         scaling["high_freq_factor"])
    old = scaling["original_max_position_embeddings"]
    wavelength = 2 * math.pi / inv
    scaled = torch.where(wavelength > old / low, inv / factor, inv)
    smooth = (old / wavelength - low) / (high - low)
    smoothed = (1 - smooth) * scaled / factor + smooth * scaled
    medium = ~(wavelength < old / high) * ~(wavelength > old / low)
    return torch.where(medium, smoothed, scaled)


def rotate(x: torch.Tensor, position: int, scaling: dict) -> torch.Tensor:
    """Rotate-half RoPE on [heads, dim]; FP32 cos/sin cast to the activation dtype."""
    inv = llama3_inv_freq(x.shape[-1], scaling).to(x.device)
    phase = inv * float(position)
    phase = torch.cat((phase, phase))
    cos, sin = phase.cos().to(x.dtype), phase.sin().to(x.dtype)
    half = x.shape[-1] // 2
    rotated = torch.cat((-x[..., half:], x[..., :half]), dim=-1)
    return x * cos + rotated * sin


def _layer(x: torch.Tensor, w: dict, layer: int, keys: torch.Tensor, values: torch.Tensor,
           position: int, heads: tuple[int, int, int], scaling: dict):
    """One Llama decoder layer for a single token; returns (x, k, v)."""
    qh, kvh, d = heads
    xn = rms(x, w["ln1"][layer], RMS_EPS)
    q = rotate((xn @ w["wq"][layer].T).view(qh, d), position, scaling)
    k = rotate((xn @ w["wk"][layer].T).view(kvh, d), position, scaling)
    v = (xn @ w["wv"][layer].T).view(kvh, d)
    ks, vs = torch.cat((keys, k[None])), torch.cat((values, v[None]))
    x = x + attention(q, ks, vs).reshape(qh * d) @ w["wo"][layer].T
    xn = rms(x, w["ln2"][layer], RMS_EPS)
    x = x + (F.silu(xn @ w["wg"][layer].T) * (xn @ w["wu"][layer].T)) @ w["wd"][layer].T
    return x, k, v


def make_inputs(case: Case, seed: int, device: str) -> dict[str, torch.Tensor]:
    p = case.params
    if p["batch"] != 1:
        raise NotImplementedError("CSM frame oracle handles batch one")
    h, layers, d = p["hidden"], p["layers"], p["head_dim"]
    dh, dlayers, dd = p["depth_hidden"], p["depth_layers"], p["depth_head_dim"]
    books, vocab = p["codebooks"], p["audio_vocab"]
    g = torch.Generator(device=device).manual_seed(seed)
    values = {
        # Mimi emits codes below 2048; 2048..2050 are CSM special ids.
        "prev_codes": torch.randint(min(vocab, 2048), (books,), generator=g,
                                    device=device, dtype=torch.int64),
        "embed_audio": random_weight(g, device, (books * vocab, h), h),
        "fnorm": random_norm(g, device, (h,)),
        "lm_head": random_weight(g, device, (vocab, h), h),
        "depth_proj": random_weight(g, device, (dh, h), h),
        "depth_fnorm": random_norm(g, device, (dh,)),
        "codebooks_head": fill_layer_weights(g, device, (books - 1, dh, vocab), dh),
    }
    for prefix, n, width, qh, kvh, hd, inter in (
            ("", layers, h, p["q_heads"], p["kv_heads"], d, p["intermediate"]),
            ("depth_", dlayers, dh, p["depth_q_heads"], p["depth_kv_heads"], dd,
             p["depth_intermediate"])):
        values[prefix + "ln1"] = random_norm(g, device, (n, width))
        values[prefix + "ln2"] = random_norm(g, device, (n, width))
        for name, rows, cols in (("wq", qh * hd, width), ("wk", kvh * hd, width),
                                 ("wv", kvh * hd, width), ("wo", width, qh * hd),
                                 ("wg", inter, width), ("wu", inter, width),
                                 ("wd", width, inter)):
            values[prefix + name] = fill_layer_weights(g, device, (n, rows, cols), cols)
    cache_shape = (layers, p["context"], p["kv_heads"], d)
    values["kcache"] = random_weight(g, device, cache_shape, d)
    values["vcache"] = random_weight(g, device, cache_shape, d)
    return values


def reference(case: Case, t: dict[str, torch.Tensor], *,
              forced_codes: torch.Tensor | None = None) -> dict[str, torch.Tensor]:
    p = case.params
    books, vocab, context = p["codebooks"], p["audio_vocab"], p["context"]
    device = t["prev_codes"].device
    offsets = torch.arange(books, device=device) * vocab
    if forced_codes is not None:
        forced_codes = forced_codes.to(device=device, dtype=torch.int64).reshape(books)
        if bool(((forced_codes < 0) | (forced_codes >= vocab)).any()):
            raise ValueError("forced codes are outside the audio vocabulary")

    # Backbone decode at position ``context``.
    x = t["embed_audio"][t["prev_codes"] + offsets].sum(0)
    backbone = {name: t[name] for name in LAYER_NAMES}
    heads = (p["q_heads"], p["kv_heads"], p["head_dim"])
    k_writes, v_writes = [], []
    for layer in range(p["layers"]):
        x, k, v = _layer(x, backbone, layer, t["kcache"][layer], t["vcache"][layer],
                         context, heads, BACKBONE_ROPE)
        k_writes.append(k)
        v_writes.append(v)
    hidden = rms(x, t["fnorm"], RMS_EPS)
    logits = [hidden @ t["lm_head"].T]

    # Depth decoder: 31 sequential greedy codebooks with a frame-local cache.
    depth = {name: t["depth_" + name] for name in LAYER_NAMES}
    dheads = (p["depth_q_heads"], p["depth_kv_heads"], p["depth_head_dim"])
    dlayers, dd = p["depth_layers"], p["depth_head_dim"]
    keys = [hidden.new_empty((0, dheads[1], dd)) for _ in range(dlayers)]
    vals = [hidden.new_empty((0, dheads[1], dd)) for _ in range(dlayers)]

    def choose(index: int) -> torch.Tensor:
        return forced_codes[index] if forced_codes is not None else logits[index].argmax()

    codes = [choose(0)]
    # Position 0 (the backbone state) only fills the cache. Position p >= 1
    # embeds code p - 1 and predicts code p.
    for position in range(books):
        if position == 0:
            embedded = hidden
        else:
            embedded = t["embed_audio"][codes[position - 1] + offsets[position - 1]]
        y = embedded @ t["depth_proj"].T
        for layer in range(dlayers):
            y, k, v = _layer(y, depth, layer, keys[layer], vals[layer], position,
                             dheads, DEPTH_ROPE)
            keys[layer] = torch.cat((keys[layer], k[None]))
            vals[layer] = torch.cat((vals[layer], v[None]))
        if position > 0:
            y = rms(y, t["depth_fnorm"], RMS_EPS)
            logits.append(y @ t["codebooks_head"][position - 1])
            codes.append(choose(position))
    return {
        "codes": torch.stack(codes).to(torch.int64),
        "logits": torch.stack(logits).float(),
        "k_write": torch.stack(k_writes).to(torch.bfloat16),
        "v_write": torch.stack(v_writes).to(torch.bfloat16),
    }
