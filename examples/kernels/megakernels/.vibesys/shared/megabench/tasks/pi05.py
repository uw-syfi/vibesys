"""Physical Intelligence pi0.5: one flow-matching action chunk at batch 1.

Untimed fixture: a synthetic PaliGemma prefill over three SigLIP images and a
right-padded prompt. As in pi0.5 the robot state is discretized into 256 bins
and written into the prompt ("Task: ..., State: 12 200 ...;\\nAction: ")
instead of entering the expert as a continuous token. Token ids are synthetic.
The prefix is bidirectional over valid tokens, and padding is masked out.
Only the per-layer BF16 prefix K/V (post-RoPE) and the prefix pad mask reach
the timed task. The VLM weights are dropped.

Timed step: ``denoise_steps`` Euler steps of the Gemma-300M action expert,
t = 1 + k*dt with dt = -1/steps, x <- x + dt*v. A sinusoidal time embedding
passes through the pi0.5 time MLP, giving the adaRMSNorm condition (scale,
shift, gate) of every expert norm. Suffix tokens are ``action_in_proj(x_t)``
with no state token. They attend to all valid prefix tokens and
bidirectionally to each other, at positions starting after the valid prefix.

Precision follows LeRobot's ``PI05Pytorch`` with ``dtype=bfloat16``: expert
projections and activations are BF16. adaRMS modulation layers, action
projections and the time MLP keep FP32 weights and activations, as do noise,
velocity and actions. Attention matches HF eager Gemma: BF16 scores and
probabilities with FP32 softmax.
"""

from __future__ import annotations

import math

import torch
import torch.nn.functional as F

from ..cases import Case
from .common import attention, fill_layer_weights, random_norm, random_weight, rms
from .vision import vision_inputs

MIN_PERIOD, MAX_PERIOD = 4e-3, 4.0
ROPE_THETA = 10_000.0
EPS = 1e-6
# openpi/LeRobot additive mask constant (big_vision's finite "-inf").
MASK_VALUE = -2.3819763e38
VOCAB = 257_152
STATE_BINS = 256
# Synthetic token ids for the pi0.5 prompt template.
PAD, BOS = 0, 2
TASK_PREFIX = (5000, 5001)            # "Task", ":"
STATE_PREFIX = (5002, 5003, 5004)     # ",", "State", ":"
PROMPT_SUFFIX = (5005, 5006, 5007, 5008)  # ";", "\n", "Action", ":"
DIGIT, SPACE = 6000, 6010             # "0".."9", " "


def prefix_length(case: Case) -> int:
    p = case.params
    return p["images"] * p["image_tokens"] + p["text_tokens"]


def _check(case: Case) -> None:
    p = case.params
    if p["batch"] != 1:
        raise ValueError("pi0.5 task is defined at batch 1")
    if p["q_heads"] % p["kv_heads"] or p["expert_hidden"] % 2:
        raise ValueError("invalid pi0.5 head geometry")
    if (p["image_size"] // p["patch_size"]) ** 2 != p["image_tokens"]:
        raise ValueError("PaliGemma uses every SigLIP patch as an image token")


def _fp32(generator: torch.Generator, device: str, shape: tuple[int, ...],
          fan_in: int) -> torch.Tensor:
    return torch.randn(shape, generator=generator, device=device) * fan_in ** -0.5


def expert_inputs(case: Case, generator: torch.Generator, device: str) -> dict[str, torch.Tensor]:
    """Gemma-300M expert (BF16) plus FP32 adaRMS, action and time projections."""
    p = case.params
    g, w, layers = generator, p["expert_hidden"], p["layers"]
    qd, kvd, inter, act = p["q_heads"] * p["head_dim"], p["kv_heads"] * p["head_dim"], \
        p["expert_intermediate"], p["action_dim"]
    values = {
        "action_in_weight": _fp32(g, device, (w, act), act),
        "action_in_bias": _fp32(g, device, (w,), w),
        "time_in_weight": _fp32(g, device, (w, w), w),
        "time_in_bias": _fp32(g, device, (w,), w),
        "time_out_weight": _fp32(g, device, (w, w), w),
        "time_out_bias": _fp32(g, device, (w,), w),
        "action_out_weight": _fp32(g, device, (act, w), w),
        "action_out_bias": _fp32(g, device, (act,), w),
    }
    for name, rows, cols in (("wq", qd, w), ("wk", kvd, w), ("wv", kvd, w),
                             ("wo", w, qd), ("wg", inter, w), ("wu", inter, w),
                             ("wd", w, inter)):
        values[name] = fill_layer_weights(g, device, (layers, rows, cols), cols)
    # Each adaRMS norm is Linear(width -> 3*width) producing scale, shift, gate.
    for name in ("attn_norm", "ffn_norm"):
        values[name + "_weight"] = _fp32(g, device, (layers, 3 * w, w), w)
        values[name + "_bias"] = _fp32(g, device, (layers, 3 * w), w)
    values["final_norm_weight"] = _fp32(g, device, (3 * w, w), w)
    values["final_norm_bias"] = _fp32(g, device, (3 * w,), w)
    return values


def vlm_inputs(case: Case, seed: int, device: str) -> dict[str, torch.Tensor]:
    """SigLIP So400m/14, PaliGemma projector and Gemma-2B prefix weights."""
    p = case.params
    values = vision_inputs(case, seed, device)
    del values["projector_norm"]  # PaliGemma's projector is a single biased linear.
    g = torch.Generator(device=device).manual_seed(seed ^ 0x9A11)
    h, layers, inter = p["hidden"], p["layers"], p["intermediate"]
    qd, kvd = p["q_heads"] * p["head_dim"], p["kv_heads"] * p["head_dim"]
    values["projector_bias"] = random_weight(g, device, (h,), h)
    values["embed"] = random_weight(g, device, (VOCAB, h), h)
    values["ln1"] = random_norm(g, device, (layers, h), gemma=True)
    values["ln2"] = random_norm(g, device, (layers, h), gemma=True)
    for name, rows, cols in (("lq", qd, h), ("lk", kvd, h), ("lv", kvd, h),
                             ("lo", h, qd), ("lg", inter, h), ("lu", inter, h),
                             ("ld", h, inter)):
        values[name] = fill_layer_weights(g, device, (layers, rows, cols), cols)
    return values


def siglip(case: Case, values: dict[str, torch.Tensor]) -> torch.Tensor:
    """SigLIP encoder, post layernorm and PaliGemma linear projector."""
    p = case.params
    heads, h = p["vision_heads"], p["vision_hidden"]
    x = F.conv2d(values["pixels"].to(torch.bfloat16), values["patch_weight"],
                 values["patch_bias"], stride=p["patch_size"])
    x = x.flatten(2).transpose(1, 2) + values["position"]
    for layer in range(p["vision_layers"]):
        normalized = F.layer_norm(x, (h,), values["norm1"][layer],
                                  values["norm1_bias"][layer], eps=1e-6)
        query, key, content = (F.linear(normalized, values[name][layer], values[name + "_bias"][layer])
                               .view(p["images"], -1, heads, h // heads).transpose(1, 2)
                               for name in ("q", "k", "v"))
        mixed = F.scaled_dot_product_attention(query, key, content).transpose(1, 2).reshape_as(x)
        x = x + F.linear(mixed, values["o"][layer], values["o_bias"][layer])
        normalized = F.layer_norm(x, (h,), values["norm2"][layer],
                                  values["norm2_bias"][layer], eps=1e-6)
        up = F.gelu(F.linear(normalized, values["up"][layer], values["up_bias"][layer]),
                    approximate="tanh")
        x = x + F.linear(up, values["down"][layer], values["down_bias"][layer])
    x = F.layer_norm(x, (h,), values["final_norm"], values["final_bias"], eps=1e-6)
    return (x @ values["projector"] + values["projector_bias"]).reshape(-1, p["hidden"])


def prompt(case: Case, seed: int, device: str) -> tuple[torch.Tensor, torch.Tensor]:
    """Synthetic tokenization of the pi0.5 prompt with a 256-bin discretized state.

    The state dimension is the padded ``action_dim`` (max_state_dim = 32).
    """
    p = case.params
    g = torch.Generator().manual_seed(seed ^ 0x7057)
    state = torch.rand(p["action_dim"], generator=g, dtype=torch.float64) * 2 - 1
    # np.digitize(state, linspace(-1, 1, 257)[:-1]) - 1
    edges = torch.linspace(-1, 1, STATE_BINS + 1, dtype=torch.float64)[:-1]
    bins = torch.bucketize(state, edges, right=True) - 1
    task = torch.randint(10_000, VOCAB, (int(torch.randint(4, 17, (), generator=g)),), generator=g)
    ids = [BOS, *TASK_PREFIX, *task.tolist(), *STATE_PREFIX]
    for index, value in enumerate(bins.tolist()):
        ids += ([SPACE] if index else []) + [DIGIT + int(d) for d in str(value)]
    # Right truncation and right padding to tokenizer_max_length, as upstream.
    ids = (ids + list(PROMPT_SUFFIX))[:p["text_tokens"]]
    valid = len(ids)
    tokens = torch.full((p["text_tokens"],), PAD, dtype=torch.int64)
    tokens[:valid] = torch.tensor(ids)
    mask = torch.arange(p["text_tokens"]) < valid
    return tokens.to(device), mask.to(device)


def prefill(case: Case, values: dict[str, torch.Tensor], embeddings: torch.Tensor,
            valid: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Gemma-2B prefix-LM prefill; return post-RoPE BF16 K/V of every layer."""
    p = case.params
    steps, d, qh, kvh = embeddings.shape[0], p["head_dim"], p["q_heads"], p["kv_heads"]
    x = embeddings.to(torch.bfloat16)
    positions = torch.cumsum(valid.long(), 0) - 1
    # Finite additive mask: padded query rows attend uniformly, as upstream.
    bias = torch.where(valid[None, :] & valid[:, None], 0.0, MASK_VALUE).to(x.device)
    keys, contents = [], []
    for layer in range(p["layers"]):
        normalized = rms(x, values["ln1"][layer], gemma=True)
        query = rope((normalized @ values["lq"][layer].T).view(steps, qh, d), positions)
        key = rope((normalized @ values["lk"][layer].T).view(steps, kvh, d), positions)
        content = (normalized @ values["lv"][layer].T).view(steps, kvh, d)
        keys.append(key)
        contents.append(content)
        groups = qh // kvh
        q, k, v = (t.transpose(0, 1) for t in (query, key.repeat_interleave(groups, 1),
                                               content.repeat_interleave(groups, 1)))
        scores = (q @ k.transpose(-1, -2)) * d ** -0.5 + bias
        mixed = (F.softmax(scores, -1, dtype=torch.float32).to(x.dtype) @ v).transpose(0, 1)
        x = x + mixed.reshape(steps, qh * d) @ values["lo"][layer].T
        if layer + 1 == p["layers"]:
            break  # The last MLP does not affect any cached K/V.
        normalized = rms(x, values["ln2"][layer], gemma=True)
        gated = F.gelu(normalized @ values["lg"][layer].T, approximate="tanh") * (normalized @ values["lu"][layer].T)
        x = x + gated @ values["ld"][layer].T
    return torch.stack(keys), torch.stack(contents)


def make_inputs(case: Case, seed: int, device: str) -> dict[str, torch.Tensor]:
    _check(case)
    p = case.params
    with torch.no_grad():
        vlm = vlm_inputs(case, seed, device)
        tokens, text_valid = prompt(case, seed, device)
        # Gemma scales token embeddings by sqrt(width), rounded to BF16.
        scale = torch.tensor(math.sqrt(p["hidden"]), device=device, dtype=torch.bfloat16)
        embeddings = torch.cat((siglip(case, vlm), vlm["embed"][tokens] * scale))
        valid = torch.cat((torch.ones(p["images"] * p["image_tokens"], dtype=torch.bool,
                                      device=device), text_valid))
        prefix_k, prefix_v = prefill(case, vlm, embeddings, valid)
        del vlm
        g = torch.Generator(device=device).manual_seed(seed ^ 0xAC71)
        values = expert_inputs(case, g, device)
        values["noise"] = torch.randn((p["horizon"], p["action_dim"]), generator=g, device=device)
    values.update(prefix_k=prefix_k, prefix_v=prefix_v, prefix_mask=valid)
    return values


def rope(x: torch.Tensor, positions: torch.Tensor) -> torch.Tensor:
    """HF ``GemmaRotaryEmbedding`` with FP32 frequencies and phases (theta 10^4).

    Bit-matches upstream LeRobot once its ``inv_freq`` buffer stays FP32.
    """
    width = x.shape[-1]
    inv = 1.0 / (ROPE_THETA ** (torch.arange(0, width, 2, dtype=torch.int64).float() / width))
    phase = positions.float()[:, None] * inv.to(x.device)[None, :]
    phase = torch.cat((phase, phase), -1)
    cos, sin = phase.cos().to(x.dtype)[:, None], phase.sin().to(x.dtype)[:, None]
    half = width // 2
    return x * cos + torch.cat((-x[..., half:], x[..., :half]), -1) * sin


def time_embedding(time: torch.Tensor, width: int) -> torch.Tensor:
    """openpi/LeRobot sine-cosine embedding, computed in FP64 then cast to FP32."""
    fraction = torch.linspace(0.0, 1.0, width // 2, dtype=torch.float64, device=time.device)
    period = MIN_PERIOD * (MAX_PERIOD / MIN_PERIOD) ** fraction
    phase = time[..., None] * (1.0 / period * 2 * math.pi)
    return torch.cat((phase.sin(), phase.cos()), -1).to(time.dtype)


def ada_rms(x: torch.Tensor, modulation: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """PiGemmaRMSNorm with a condition: FP32 normalize/scale/shift, BF16 out and gate."""
    normalized = x * torch.rsqrt(x.float().square().mean(-1, keepdim=True) + EPS)
    scale, shift, gate = modulation.chunk(3, -1)
    return (normalized * (1 + scale.float()) + shift.float()).to(x.dtype), gate.to(x.dtype)


def time_condition(case: Case, values: dict[str, torch.Tensor], time: torch.Tensor) -> torch.Tensor:
    """pi0.5 time MLP: silu(out(silu(in(sincos(t))))), the adaRMS condition."""
    cond = time_embedding(time, case.params["expert_hidden"])
    cond = F.silu(F.linear(cond, values["time_in_weight"], values["time_in_bias"]))
    return F.silu(F.linear(cond, values["time_out_weight"], values["time_out_bias"]))


def expert_attention(case: Case, values: dict[str, torch.Tensor], layer: int, normalized: torch.Tensor,
                     positions: torch.Tensor, allowed: torch.Tensor) -> torch.Tensor:
    """Expert queries over [prefix K/V ; suffix K/V], then o_proj (BF16)."""
    p = case.params
    tokens, d, qh, kvh = normalized.shape[0], p["head_dim"], p["q_heads"], p["kv_heads"]
    query = rope((normalized @ values["wq"][layer].T).view(tokens, qh, d), positions)
    key = rope((normalized @ values["wk"][layer].T).view(tokens, kvh, d), positions)
    content = (normalized @ values["wv"][layer].T).view(tokens, kvh, d)
    key = torch.cat((values["prefix_k"][layer], key))
    content = torch.cat((values["prefix_v"][layer], content))
    mixed = attention(query, key, content, allowed=allowed).reshape(tokens, qh * d)
    return mixed @ values["wo"][layer].T


def expert(case: Case, values: dict[str, torch.Tensor], h: torch.Tensor, cond: torch.Tensor,
           positions: torch.Tensor, allowed: torch.Tensor) -> torch.Tensor:
    """Gemma-300M layers with adaRMS norms and gated residuals; final adaRMS norm."""
    for layer in range(case.params["layers"]):
        normalized, gate = ada_rms(h, F.linear(cond, values["attn_norm_weight"][layer],
                                               values["attn_norm_bias"][layer]))
        h = h + expert_attention(case, values, layer, normalized, positions, allowed) * gate
        normalized, gate = ada_rms(h, F.linear(cond, values["ffn_norm_weight"][layer],
                                               values["ffn_norm_bias"][layer]))
        gated = F.gelu(normalized @ values["wg"][layer].T, approximate="tanh") * (normalized @ values["wu"][layer].T)
        h = h + (gated @ values["wd"][layer].T) * gate
    return ada_rms(h, F.linear(cond, values["final_norm_weight"], values["final_norm_bias"]))[0]


def velocity(case: Case, values: dict[str, torch.Tensor], x: torch.Tensor, time: torch.Tensor,
             positions: torch.Tensor, allowed: torch.Tensor) -> torch.Tensor:
    cond = time_condition(case, values, time)
    h = F.linear(x, values["action_in_weight"], values["action_in_bias"]).to(torch.bfloat16)
    h = expert(case, values, h, cond, positions, allowed)
    return F.linear(h.float(), values["action_out_weight"], values["action_out_bias"])


def suffix_layout(case: Case, mask: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Suffix positions after the valid prefix, and its [horizon, prefix+horizon] mask."""
    horizon = case.params["horizon"]
    positions = mask.sum() + torch.arange(horizon, device=mask.device)
    allowed = torch.cat((mask[None, :].expand(horizon, -1),
                         torch.ones((horizon, horizon), dtype=torch.bool, device=mask.device)), 1)
    return positions, allowed


def reference(case: Case, values: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    _check(case)
    p = case.params
    steps, device = p["denoise_steps"], values["noise"].device
    positions, allowed = suffix_layout(case, values["prefix_mask"])
    dt = -1.0 / steps
    x = values["noise"]
    for step in range(steps):
        time = torch.full((1,), 1.0 + step * dt, dtype=torch.float32, device=device)
        x = x + dt * velocity(case, values, x, time, positions, allowed)
    return {"actions": x}
