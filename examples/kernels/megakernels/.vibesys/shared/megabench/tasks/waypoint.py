"""Overworld Waypoint-1.5-1B: generate one latent frame of a causal world model.

Waypoint is an autoregressive rectified-flow diffusion transformer. Every
frame is one 2x2-patchified latent (``tokens_per_frame`` tokens). The timed
step generates the latent of frame ``f = frame_index`` from Gaussian noise:

1. Four denoise passes with sigmas ``SIGMAS[:-1]``. Each pass runs the full
   DiT over the frame's tokens and updates ``x = x + (sigma_next - sigma) * v``
   in BF16. The caches stay frozen during these passes.
2. One cache pass over the clean latent at sigma 0. Its per-layer K/V are the
   ``k_write``/``v_write`` outputs; its image output is discarded.

The previous frames are inputs only through the per-layer K/V caches.
Upstream: ``Overworld/Waypoint-1.5-1B`` revision ``391f9282``
(``transformer/model.py``, ``modular_blocks.py``). This module was written
from the model's semantics; it contains no upstream code.

Block ``l`` (D = hidden, H heads, G = H / kv_heads, d = head_dim)::

    cond  = noise_mlp(fourier(1000 * sigma))          # FP32 MLP, BF16 result
    s0,b0,g0 = W_attn_cond[l] @ silu(cond + attn_cond_bias[l])   # one token
    s1,b1,g1 = W_mlp_cond[l]  @ silu(cond + mlp_cond_bias[l])
    h = rms(x) * (1 + s0) + b0
    q, k, v = h Wq^T, h Wk^T, h Wv^T
    v = lerp(v, v_first, v_lamb[l])        # v_first: layer 0's v (value residual)
    q, k = rope(rms(q)), rope(rms(k))      # per-head RMS, no weight
    x = x + g0 * Wo attention(q, [visible cache slots ; k], [... ; v])
    if l % control_period == 0:            # controller fusion layers
        x = x + Wf2 silu(Wfx rms(x) + Wfc rms(ctrl))
    x = x + g1 * fc2 silu(fc1(rms(x) * (1 + s1) + b1))
    out = unpatchify(silu(rms(x) * (1 + a) + c)),  [a; c] = W_out silu(cond)

``rms`` has no weight and uses eps = FP32 epsilon (upstream ``F.rms_norm``
with ``eps=None`` on BF16 input). Attention is non-causal within the frame,
scale ``d ** -0.5``, and GQA query head ``h`` reads KV head ``h // G``.

RoPE (``ortho``) rotates interleaved pairs ``(2i, 2i+1)`` but writes the
result as ``[rotated evens ; rotated odds]``. Cached keys use that layout.
Pair frequencies are ``[x * fxy, y * fxy, t * ft]`` (d/8, d/8 and d/4
entries), where token ``n`` sits at grid ``(y, x) = divmod(n, grid_width)``,
the spatial coordinates are ``(2 * pos + 1) / extent - 1``, ``t = f * TS_MULT``,
``fxy = pi * repeat2(linspace(1, 0.4 * min(grid_h, grid_w), d/16))``, and
``ft = repeat2(ROPE_THETA ** (-arange(0, d/4, 2) / (d/4)))``.

Cache layout (inputs ``k_cache``/``v_cache``, BF16
``[layers, slots, tokens_per_frame, kv_heads, head_dim]``): every layer is a
ring of frame slots. Layer ``l`` is global if
``(l - GLOBAL_ATTN_OFFSET) % global_period == 0`` (layers 3, 7, ..., 23).

* Local layers: dilation 1, ``local_window`` slots. Frame ``g`` lives in
  slot ``g % local_window``.
* Global layers: dilation ``global_dilation``, ``global_window //
  global_dilation`` slots. Only frames ``g`` with ``g % dilation == 0`` are
  stored, in slot ``(g // dilation) % slots``.

``slots`` is the larger of the two ring sizes. Slots past a layer's ring
size, and slots that no earlier frame wrote, are zero and never visible.
Frame ``f`` attends to every written slot of its layer, plus its own tokens.
The exception is that when ``f % dilation == 0``, the slot that ``f`` is
about to overwrite (slot ``ceil(f / dilation) % ring``) is hidden. At
``f = 128`` the local layers see frames 113..127 and the global layers see
frames 8, 16, ..., 120, each plus the current frame. Both rings overwrite
slot 0 (frames 112 and 0).

Outputs: ``latent`` BF16 ``[C, H, W]``; ``k_write``/``v_write`` BF16
``[layers, tokens_per_frame, kv_heads, head_dim]``, which hold post-RMS,
post-RoPE keys and post-value-residual values from the sigma-0 pass. A
global layer persists ``k_write`` only when ``f % dilation == 0``. Tokens
are ordered row-major over the ``(H / patch, W / patch)`` grid.
"""

from __future__ import annotations

import math

import torch
import torch.nn.functional as F

from ..cases import Case
from .common import fill_layer_weights, random_weight


# config.yaml scheduler_sigmas [1.0, 0.9, 0.75, 0.3, 0.0], as the BF16 tensor
# upstream builds (transformer dtype). Their differences are exact in BF16.
SIGMAS = (1.0, 0.8984375, 0.75, 0.30078125, 0.0)
GLOBAL_ATTN_OFFSET = -1
# base_fps 15 // (inference_fps 60 / temporal_compression 4)
TS_MULT = 1
ROPE_THETA = 10000.0
ROPE_NYQUIST_FRAC = 0.8
NOISE_FOURIER_DIM = 512
NOISE_FOURIER_BASE = 10000.0
NOISE_HIDDEN_MULT = 4
MOUSE_DIMS = 2
SCROLL_DIMS = 1
RMS_EPS = torch.finfo(torch.float32).eps


def geometry(case: Case) -> dict[str, int]:
    p = case.params
    if p["batch"] != 1:
        raise NotImplementedError("Waypoint generates one frame at batch one")
    if p["hidden"] != p["q_heads"] * p["head_dim"] or p["head_dim"] % 8:
        raise ValueError("Waypoint needs hidden = q_heads * head_dim and head_dim % 8 == 0")
    if p["q_heads"] % p["kv_heads"]:
        raise ValueError("q_heads must be a multiple of kv_heads")
    if p["latent_height"] % p["patch"] or p["latent_width"] % p["patch"]:
        raise ValueError("latent grid must be divisible by the patch")
    grid_h, grid_w = p["latent_height"] // p["patch"], p["latent_width"] // p["patch"]
    if grid_h * grid_w != p["tokens_per_frame"]:
        raise ValueError(f"{grid_h}x{grid_w} patches != tokens_per_frame {p['tokens_per_frame']}")
    if p["global_window"] % p["global_dilation"]:
        raise ValueError("global_window must be a multiple of global_dilation")
    if p["denoise_steps"] != len(SIGMAS) - 1:
        raise ValueError(f"the upstream schedule has {len(SIGMAS) - 1} denoise steps")
    return {"grid_h": grid_h, "grid_w": grid_w,
            "slots": max(p["local_window"], p["global_window"] // p["global_dilation"])}


def is_global(case: Case, layer: int) -> bool:
    period = case.params["global_period"]
    return (layer - (GLOBAL_ATTN_OFFSET % period)) % period == 0


def is_control(case: Case, layer: int) -> bool:
    return layer % case.params["control_period"] == 0


def control_layers(case: Case) -> list[int]:
    return [layer for layer in range(case.params["layers"]) if is_control(case, layer)]


def ring(case: Case, layer: int) -> tuple[int, int]:
    """(slots, dilation) of a layer's cache ring."""
    p = case.params
    if is_global(case, layer):
        return p["global_window"] // p["global_dilation"], p["global_dilation"]
    return p["local_window"], 1


def write_slot(case: Case, layer: int, frame: int) -> int | None:
    """Slot frame ``frame`` persists into, or None when the layer skips it."""
    slots, dilation = ring(case, layer)
    if frame % dilation:
        return None
    return (frame // dilation) % slots


def slot_frames(case: Case, layer: int, frame: int) -> list[int | None]:
    """Frame held by each ring slot when ``frame`` is generated."""
    slots, dilation = ring(case, layer)
    held: list[int | None] = [None] * slots
    first = max(0, (frame - 1) // dilation - slots + 1) * dilation
    for g in range(first, frame, dilation):
        held[(g // dilation) % slots] = g
    return held


def visible_slots(case: Case, layer: int, frame: int) -> list[int]:
    hidden = write_slot(case, layer, frame)
    return [slot for slot, g in enumerate(slot_frames(case, layer, frame))
            if g is not None and slot != hidden]


def _rms(x: torch.Tensor) -> torch.Tensor:
    xf = x.float()
    return (xf * torch.rsqrt(xf.square().mean(-1, keepdim=True) + RMS_EPS)).to(x.dtype)


def _rope_tables(case: Case, frame: int, device) -> tuple[torch.Tensor, torch.Tensor]:
    geo, d = geometry(case), case.params["head_dim"]
    d_xy, d_t = d // 8, d // 4
    max_freq = min(geo["grid_h"], geo["grid_w"]) * ROPE_NYQUIST_FRAC
    xy = (torch.linspace(1.0, max_freq / 2, (d_xy + 1) // 2, dtype=torch.float32, device=device)
          * torch.pi).repeat_interleave(2)[:d_xy]
    inv_t = 1.0 / (ROPE_THETA ** (torch.arange(0, d_t, 2, dtype=torch.float32, device=device) / d_t))
    inv_t = inv_t.repeat_interleave(2)
    token = torch.arange(case.params["tokens_per_frame"], device=device)
    xs = (2.0 * (token % geo["grid_w"]).float() + 1.0) / geo["grid_w"] - 1.0
    ys = (2.0 * (token // geo["grid_w"]).float() + 1.0) / geo["grid_h"] - 1.0
    ts = torch.full_like(xs, float(frame * TS_MULT))
    phase = torch.cat((xs[:, None] * xy, ys[:, None] * xy, ts[:, None] * inv_t), -1)
    # [tokens, 1, d/2], broadcast over heads.
    return phase.cos()[:, None], phase.sin()[:, None]


def _rope(x: torch.Tensor, table: tuple[torch.Tensor, torch.Tensor]) -> torch.Tensor:
    cos, sin = table
    xf = x.float()
    even, odd = xf[..., 0::2], xf[..., 1::2]
    return torch.cat((even * cos - odd * sin, odd * cos + even * sin), -1).to(x.dtype)


def _attention(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
    """[T, H, d] queries over [S, Hkv, d] keys; FP32 scores and softmax."""
    tokens, heads, d = q.shape
    kv_heads = k.shape[1]
    group = heads // kv_heads
    qh = q.float().permute(1, 0, 2).reshape(kv_heads, group * tokens, d)
    scores = qh @ k.float().permute(1, 2, 0) * d ** -0.5
    probs = torch.softmax(scores, -1)
    out = (probs @ v.float().permute(1, 0, 2)).to(q.dtype)
    return out.reshape(heads, tokens, d).permute(1, 0, 2)


def _noise_embedding(w: dict, sigma: float, device) -> torch.Tensor:
    half = NOISE_FOURIER_DIM // 2
    freq = torch.logspace(0, -1, steps=half, base=NOISE_FOURIER_BASE,
                          dtype=torch.float32, device=device)
    s = torch.full((1,), sigma, dtype=torch.bfloat16, device=device).float() * 1000
    phase = s[:, None] * freq[None, :]
    emb = torch.cat((phase.sin(), phase.cos()), -1) * 2 ** 0.5
    emb = F.silu(emb @ w["noise_fc1"].T) @ w["noise_fc2"].T
    return emb[0].to(torch.bfloat16)


def _control_embedding(w: dict) -> torch.Tensor:
    x = torch.cat((w["mouse"], w["button"], w["scroll"]))
    return F.silu(x @ w["ctrl_fc1"].T) @ w["ctrl_fc2"].T


def _patchify(case: Case, w: dict, latent: torch.Tensor) -> torch.Tensor:
    patch = case.params["patch"]
    tokens = F.conv2d(latent[None], w["patch_w"], stride=patch)
    return tokens.flatten(2)[0].T


def _forward(case: Case, w: dict, latent: torch.Tensor, sigma: float, frame: int,
             ctrl: torch.Tensor, k_cache: torch.Tensor, v_cache: torch.Tensor,
             *, kv_only: bool = False):
    """One DiT pass. Returns (velocity or None, k [L,T,Hkv,d], v [L,T,Hkv,d])."""
    p = case.params
    heads, kv_heads, d = p["q_heads"], p["kv_heads"], p["head_dim"]
    device = latent.device
    cond = _noise_embedding(w, sigma, device)
    table = _rope_tables(case, frame, device)
    ctrl_n = _rms(ctrl)
    fusion = {layer: index for index, layer in enumerate(control_layers(case))}
    x = _patchify(case, w, latent)
    tokens = x.shape[0]
    ks, vs, v_first = [], [], None
    for layer in range(p["layers"]):
        attn_h = F.silu(cond + w["attn_cond_bias"][layer])
        s0, b0, g0 = (attn_h @ w["attn_cond_w"][layer, i].T for i in range(3))
        h = _rms(x) * (1 + s0) + b0
        q = (h @ w["wq"][layer].T).view(tokens, heads, d)
        k = (h @ w["wk"][layer].T).view(tokens, kv_heads, d)
        v = (h @ w["wv"][layer].T).view(tokens, kv_heads, d)
        if v_first is None:
            v_first = v
        v = torch.lerp(v, v_first, w["v_lamb"][layer])
        q, k = _rope(_rms(q), table), _rope(_rms(k), table)
        ks.append(k)
        vs.append(v)
        if kv_only and layer == p["layers"] - 1:
            break
        visible = visible_slots(case, layer, frame)
        keys = torch.cat((k_cache[layer, visible].reshape(-1, kv_heads, d), k))
        values = torch.cat((v_cache[layer, visible].reshape(-1, kv_heads, d), v))
        attn = _attention(q, keys, values).reshape(tokens, heads * d)
        x = (attn @ w["wo"][layer].T) * g0 + x
        if layer in fusion:
            i = fusion[layer]
            fused = F.silu(_rms(x) @ w["fuse_x"][i].T + ctrl_n @ w["fuse_c"][i].T)
            x = fused @ w["fuse_out"][i].T + x
        mlp_h = F.silu(cond + w["mlp_cond_bias"][layer])
        s1, b1, g1 = (mlp_h @ w["mlp_cond_w"][layer, i].T for i in range(3))
        mlp = F.silu((_rms(x) * (1 + s1) + b1) @ w["fc1"][layer].T) @ w["fc2"][layer].T
        x = mlp * g1 + x
    k_out, v_out = torch.stack(ks), torch.stack(vs)
    if kv_only:
        return None, k_out, v_out
    a, c = (F.silu(cond) @ w["out_norm_w"].T).chunk(2)
    x = F.silu(_rms(x) * (1 + a) + c)
    geo = geometry(case)
    grid = x.T.reshape(1, p["hidden"], geo["grid_h"], geo["grid_w"])
    out = F.conv_transpose2d(grid, w["unpatch_w"], w["unpatch_b"], stride=p["patch"])
    return out[0], k_out, v_out


def _random_controller(gen: torch.Generator, case: Case, device) -> dict[str, torch.Tensor]:
    buttons = case.params["buttons"]
    pressed = torch.rand(buttons, generator=gen, device=device) < 4.0 / buttons
    return {
        "mouse": torch.randn(MOUSE_DIMS, generator=gen, device=device).to(torch.bfloat16),
        "button": pressed.to(torch.bfloat16),
        "scroll": torch.randint(-1, 2, (SCROLL_DIMS,), generator=gen,
                                device=device).to(torch.bfloat16),
    }


def make_weights(case: Case, gen: torch.Generator, device) -> dict[str, torch.Tensor]:
    p = case.params
    D, L, C, P = p["hidden"], p["layers"], p["latent_channels"], p["patch"]
    inner = D * p["mlp_ratio"]
    qdim, kvdim = p["q_heads"] * p["head_dim"], p["kv_heads"] * p["head_dim"]
    nf = len(control_layers(case))
    noise_hidden = NOISE_HIDDEN_MULT * D
    ctrl_in = MOUSE_DIMS + p["buttons"] + SCROLL_DIMS

    def fp32(*shape, fan_in):
        return torch.randn(shape, generator=gen, device=device) * fan_in ** -0.5

    def bias(*shape):
        return (0.1 * torch.randn(shape, generator=gen, device=device)).to(torch.bfloat16)

    return {
        "patch_w": random_weight(gen, device, (D, C, P, P), C * P * P),
        # Upstream keeps denoise_step_emb in FP32 (_keep_in_fp32_modules).
        "noise_fc1": fp32(noise_hidden, NOISE_FOURIER_DIM, fan_in=NOISE_FOURIER_DIM),
        "noise_fc2": fp32(D, noise_hidden, fan_in=noise_hidden),
        "ctrl_fc1": random_weight(gen, device, (inner, ctrl_in), ctrl_in),
        "ctrl_fc2": random_weight(gen, device, (D, inner), inner),
        "attn_cond_bias": bias(L, D),
        "attn_cond_w": fill_layer_weights(gen, device, (L * 3, D, D), D).view(L, 3, D, D),
        "mlp_cond_bias": bias(L, D),
        "mlp_cond_w": fill_layer_weights(gen, device, (L * 3, D, D), D).view(L, 3, D, D),
        "wq": fill_layer_weights(gen, device, (L, qdim, D), D),
        "wk": fill_layer_weights(gen, device, (L, kvdim, D), D),
        "wv": fill_layer_weights(gen, device, (L, kvdim, D), D),
        "wo": fill_layer_weights(gen, device, (L, D, qdim), qdim),
        "v_lamb": (0.5 + 0.15 * torch.randn(L, generator=gen, device=device)).to(torch.bfloat16),
        "fuse_x": fill_layer_weights(gen, device, (nf, D, D), D),
        "fuse_c": fill_layer_weights(gen, device, (nf, D, D), D),
        "fuse_out": fill_layer_weights(gen, device, (nf, D, D), D),
        "fc1": fill_layer_weights(gen, device, (L, inner, D), D),
        "fc2": fill_layer_weights(gen, device, (L, D, inner), inner),
        "out_norm_w": random_weight(gen, device, (2 * D, D), D),
        "unpatch_w": random_weight(gen, device, (D, C, P, P), D),
        "unpatch_b": bias(C),
    }


def build_cache(case: Case, weights: dict, gen: torch.Generator, device,
                frames: int) -> tuple[torch.Tensor, torch.Tensor]:
    """Commit ``frames`` seeded clean latents through sigma-0 cache passes.

    This is the upstream generation history with random clean frames: each
    frame attends to the ring as it stood, then persists into its slot.
    Denoise passes never write the cache, so they are skipped.
    """
    p = case.params
    shape = (p["layers"], geometry(case)["slots"], p["tokens_per_frame"],
             p["kv_heads"], p["head_dim"])
    k_cache = torch.zeros(shape, dtype=torch.bfloat16, device=device)
    v_cache = torch.zeros(shape, dtype=torch.bfloat16, device=device)
    latent_shape = (p["latent_channels"], p["latent_height"], p["latent_width"])
    for g in range(frames):
        latent = torch.randn(latent_shape, generator=gen, device=device).to(torch.bfloat16)
        ctrl = _control_embedding(weights | _random_controller(gen, case, device))
        _, ks, vs = _forward(case, weights, latent, 0.0, g, ctrl, k_cache, v_cache, kv_only=True)
        for layer in range(p["layers"]):
            slot = write_slot(case, layer, g)
            if slot is not None:
                k_cache[layer, slot] = ks[layer]
                v_cache[layer, slot] = vs[layer]
    return k_cache, v_cache


def make_inputs(case: Case, seed: int, device: str) -> dict[str, torch.Tensor]:
    p = case.params
    geometry(case)
    gen = torch.Generator(device=device).manual_seed(seed)
    with torch.no_grad():
        weights = make_weights(case, gen, device)
        k_cache, v_cache = build_cache(case, weights, gen, device, p["frame_index"])
        step = _random_controller(gen, case, device)
        noise = torch.randn((p["latent_channels"], p["latent_height"], p["latent_width"]),
                            generator=gen, device=device).to(torch.bfloat16)
    return {"noise": noise, **step, "k_cache": k_cache, "v_cache": v_cache, **weights}


def reference(case: Case, t: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    frame = case.params["frame_index"]
    ctrl = _control_embedding(t)
    x = t["noise"]
    for sigma, sigma_next in zip(SIGMAS[:-1], SIGMAS[1:]):
        velocity, _, _ = _forward(case, t, x, sigma, frame, ctrl, t["k_cache"], t["v_cache"])
        x = x + (sigma_next - sigma) * velocity
    _, k_write, v_write = _forward(case, t, x, SIGMAS[-1], frame, ctrl,
                                   t["k_cache"], t["v_cache"], kv_only=True)
    return {"latent": x, "k_write": k_write, "v_write": v_write}


def step_flops(case: Case) -> dict[str, int]:
    """Matmul FLOPs: four full passes plus the K/V-only sigma-0 pass.

    The cache pass needs no attention, MLP or output head after the last
    layer's K/V projection. One-token conditioning matmuls are omitted.
    """
    p = case.params
    D, T, d, L = p["hidden"], p["tokens_per_frame"], p["head_dim"], p["layers"]
    inner = D * p["mlp_ratio"]
    qdim, kvdim = p["q_heads"] * d, p["kv_heads"] * d
    patch = 2 * T * D * p["latent_channels"] * p["patch"] ** 2
    qkv = 2 * T * D * (qdim + 2 * kvdim)
    rest = 2 * T * (qdim * D + 2 * D * inner)
    attn = [4 * T * p["q_heads"] * d * T * (len(visible_slots(case, layer, p["frame_index"])) + 1)
            for layer in range(L)]
    fusion = [4 * T * D * D if is_control(case, layer) else 0 for layer in range(L)]
    full = 2 * patch + L * (qkv + rest) + sum(attn) + sum(fusion)
    kv_pass = patch + L * qkv + (L - 1) * rest + sum(attn[:-1]) + sum(fusion[:-1])
    return {"per_full_pass": full, "kv_pass": kv_pass,
            "total": p["denoise_steps"] * full + kv_pass}
