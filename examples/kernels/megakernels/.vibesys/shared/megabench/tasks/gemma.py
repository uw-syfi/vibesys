"""Gemma 3 4B text decode with explicit W8A16 or packed W4A16 weights.

This is a synthetic-weight tier. Every projection uses symmetric per-output-row
quantization; embedding and norm parameters stay BF16. The same random base
weight stream is used for the W8 and W4 cells at a given seed.

With ``batch == 1`` and no ``contexts`` the layout is one sequence: ``token``
is a scalar and the caches are ``[layers, context, kv_heads, head_dim]``.
Otherwise it is a batch of B sequences with ragged cache lengths: ``token``
is ``[B]``, the caches are ``[layers, B, context, kv_heads, head_dim]``
(padded), and ``context_lens[B]`` (int32) gives each sequence's cached
length. Sequence b attends its cached positions below ``context_lens[b]``
(sliding-window layers only the last ``local_window - 1`` of them) and its
own new key at position ``context_lens[b]``. Outputs gain a leading B axis
(``k_write``/``v_write`` are ``[layers, B, kv_heads, head_dim]``).

With ``tokens > 1`` the step is a prefill chunk of one sequence: ``tokens`` is
``[T]`` (int64) at positions ``context .. context + T - 1`` on top of the
cached ``[layers, context, kv_heads, head_dim]``. Attention is causal, and
sliding-window layers see the last ``local_window`` positions including the
query's own. Outputs: ``logits[vocab]`` and ``next_token`` for the last
position only, and ``k_write``/``v_write`` ``[layers, T, kv_heads, head_dim]``.
"""

from __future__ import annotations

import math

import torch
import torch.nn.functional as F

from ..cases import Case
from .common import attention, fill_layer_weights, random_norm, random_weight, rms, rope


LINEAR_NAMES = ("wq", "wk", "wv", "wo", "wg", "wu", "wd")
NORM_NAMES = ("ln1", "ln2", "ln3", "ln4", "qn", "kn", "fnorm")


def _quantized_stack(g: torch.Generator, device: str, shape: tuple[int, int, int],
                     bits: int) -> tuple[torch.Tensor, torch.Tensor]:
    layers, rows, cols = shape
    if bits == 4 and cols % 2:
        raise ValueError("W4 input dimension must be even")
    packed_cols = cols if bits == 8 else cols // 2
    qweight = torch.empty((layers, rows, packed_cols), device=device,
                          dtype=torch.int8 if bits == 8 else torch.uint8)
    scales = torch.empty((layers, rows), device=device, dtype=torch.float32)
    limit = 127 if bits == 8 else 7
    for layer in range(layers):
        base = random_weight(g, device, (rows, cols), cols).float()
        scale = (base.abs().amax(-1) / limit).clamp_min(1e-8)
        quant = torch.round(base / scale[:, None]).clamp(-limit, limit).to(torch.int8)
        if bits == 8:
            qweight[layer].copy_(quant)
        else:
            lo = (quant[:, 0::2].to(torch.int16) + 8).to(torch.uint8)
            hi = (quant[:, 1::2].to(torch.int16) + 8).to(torch.uint8)
            qweight[layer].copy_(lo | (hi << 4))
        scales[layer].copy_(scale)
    return qweight, scales


def dequant(qweight: torch.Tensor, scale: torch.Tensor, bits: int) -> torch.Tensor:
    if bits == 8:
        q = qweight.float()
    elif bits == 4:
        lo = (qweight & 15).to(torch.int16) - 8
        hi = (qweight >> 4).to(torch.int16) - 8
        q = torch.stack((lo, hi), dim=-1).flatten(-2).float()
    else:
        raise ValueError(f"unsupported quantization width {bits}")
    return q * scale.float().unsqueeze(-1)


def _batched(case: Case) -> bool:
    return case.params["batch"] != 1 or bool(case.contexts)


def _prefill(case: Case) -> bool:
    return case.params.get("tokens", 1) > 1


def make_inputs(case: Case, seed: int, device: str) -> dict[str, torch.Tensor]:
    p = case.params
    batched, batch = _batched(case), p["batch"]
    if _prefill(case) and batched:
        raise NotImplementedError("Gemma 3 prefill handles one sequence")
    h, layers, inter, d = (p["hidden"], p["layers"], p["intermediate"],
                           p["head_dim"])
    qdim, kdim = p["q_heads"] * d, p["kv_heads"] * d
    g = torch.Generator(device=device).manual_seed(seed)
    ids = ((p["tokens"],) if _prefill(case) else (batch,) if batched else ())
    values = {
        "tokens" if _prefill(case) else "token": torch.randint(
            p["vocab"], ids, generator=g, device=device, dtype=torch.int64),
        "embed": random_weight(g, device, (p["vocab"], h), h),
        "ln1": random_norm(g, device, (layers, h), gemma=True),
        "ln2": random_norm(g, device, (layers, h), gemma=True),
        "ln3": random_norm(g, device, (layers, h), gemma=True),
        "ln4": random_norm(g, device, (layers, h), gemma=True),
        "qn": random_norm(g, device, (layers, d), gemma=True),
        "kn": random_norm(g, device, (layers, d), gemma=True),
        "fnorm": random_norm(g, device, (h,), gemma=True),
    }
    shapes = {
        "wq": (layers, qdim, h), "wk": (layers, kdim, h),
        "wv": (layers, kdim, h), "wo": (layers, h, qdim),
        "wg": (layers, inter, h), "wu": (layers, inter, h),
        "wd": (layers, h, inter),
    }
    for name, shape in shapes.items():
        if p["bits"] == 16:
            values[name] = fill_layer_weights(g, device, shape, shape[-1])
        else:
            values[name], values[f"{name}_scale"] = _quantized_stack(
                g, device, shape, p["bits"])
    cache_shape = ((layers, batch, p["context"], p["kv_heads"], d) if batched
                   else (layers, p["context"], p["kv_heads"], d))
    values["kcache"] = random_weight(g, device, cache_shape, d)
    values["vcache"] = random_weight(g, device, cache_shape, d)
    if batched:
        lens = case.contexts or (p["context"],) * batch
        values["context_lens"] = torch.tensor(lens, dtype=torch.int32, device=device)
    return values


def _matrix(values: dict[str, torch.Tensor], name: str, layer: int, bits: int,
            dtype: torch.dtype) -> torch.Tensor:
    # Packed input weights and FP32 scales retain their native formats.
    # W8A16/W4A16 projections consume BF16 decoded weights/activations.
    return (values[name][layer] if bits == 16 else
            dequant(values[name][layer], values[f"{name}_scale"][layer], bits).to(dtype))


def reference(case: Case, values: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    if _batched(case):
        return _batched_reference(case, values)
    if _prefill(case):
        return _prefill_reference(case, values)
    p = case.params
    h, d, context, bits = (p["hidden"], p["head_dim"], p["context"], p["bits"])
    qh, kvh = p["q_heads"], p["kv_heads"]
    x = values["embed"][values["token"]] * torch.tensor(
        math.sqrt(h), device=values["token"].device, dtype=torch.bfloat16)
    positions = torch.tensor([context], device=x.device)
    k_writes, v_writes = [], []

    def linear(name: str, layer: int, vec: torch.Tensor) -> torch.Tensor:
        return _matrix(values, name, layer, bits, vec.dtype) @ vec

    for layer in range(p["layers"]):
        xn = rms(x, values["ln1"][layer], gemma=True)
        q = linear("wq", layer, xn).view(qh, d)
        k = linear("wk", layer, xn).view(kvh, d)
        v = linear("wv", layer, xn).view(kvh, d)
        q = rms(q, values["qn"][layer], gemma=True)
        k = rms(k, values["kn"][layer], gemma=True)
        local = (layer + 1) % 6 != 0
        theta = 10_000.0 if local else 1_000_000.0
        factor = 1.0 if local else 8.0
        q = rope(q, positions, theta, factor=factor)
        k = rope(k, positions, theta, factor=factor)
        k_writes.append(k.to(torch.bfloat16))
        v_writes.append(v.to(torch.bfloat16))
        start = max(0, context + 1 - p["local_window"]) if local else 0
        ks = torch.cat((values["kcache"][layer, start:], k[None]), dim=0)
        vs = torch.cat((values["vcache"][layer, start:], v[None]), dim=0)
        attn = attention(q, ks, vs, scale=p.get("query_pre_attn_scalar", d) ** -0.5).reshape(qh * d)
        x = x + rms(linear("wo", layer, attn), values["ln2"][layer], gemma=True)

        xn = rms(x, values["ln3"][layer], gemma=True)
        gate = linear("wg", layer, xn)
        up = linear("wu", layer, xn)
        mlp = linear("wd", layer, F.gelu(gate, approximate="tanh") * up)
        x = x + rms(mlp, values["ln4"][layer], gemma=True)

    logits = values["embed"] @ rms(x, values["fnorm"], gemma=True)
    return {
        "logits": logits.float(),
        "next_token": logits.argmax().to(torch.int64),
        "k_write": torch.stack(k_writes),
        "v_write": torch.stack(v_writes),
    }


def _batched_reference(case: Case, values: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    """The same step for B sequences at once; the caches are not modified."""
    p = case.params
    h, d, bits, window = p["hidden"], p["head_dim"], p["bits"], p["local_window"]
    qh, kvh = p["q_heads"], p["kv_heads"]
    tokens = values["token"]
    batch, device = tokens.shape[0], tokens.device
    width = values["kcache"].shape[2]  # static, so the step stays CUDA-graph capturable
    lens = values["context_lens"].to(device).long()
    slots = torch.arange(width, device=device)[None]
    cached = slots < lens[:, None]
    new = torch.ones(batch, 1, dtype=torch.bool, device=device)
    visible = {False: torch.cat((cached, new), 1)[:, None, None],
               True: torch.cat((cached & (slots >= lens[:, None] + 1 - window), new), 1)[:, None, None]}
    # A fill, not a host copy, so the step stays CUDA-graph capturable.
    x = values["embed"][tokens] * torch.full((), math.sqrt(h), device=device, dtype=torch.bfloat16)
    scale = p.get("query_pre_attn_scalar", d) ** -0.5
    k_writes, v_writes = [], []

    def linear(name: str, layer: int, rows: torch.Tensor) -> torch.Tensor:
        return rows @ _matrix(values, name, layer, bits, rows.dtype).T

    for layer in range(p["layers"]):
        xn = rms(x, values["ln1"][layer], gemma=True)
        q = linear("wq", layer, xn).view(batch, qh, d)
        k = linear("wk", layer, xn).view(batch, kvh, d)
        v = linear("wv", layer, xn).view(batch, kvh, d)
        q = rms(q, values["qn"][layer], gemma=True)
        k = rms(k, values["kn"][layer], gemma=True)
        local = (layer + 1) % 6 != 0
        theta = 10_000.0 if local else 1_000_000.0
        factor = 1.0 if local else 8.0
        q = rope(q, lens, theta, factor=factor)
        k = rope(k, lens, theta, factor=factor)
        k_writes.append(k.to(torch.bfloat16))
        v_writes.append(v.to(torch.bfloat16))
        keys = torch.cat((values["kcache"][layer], k[:, None]), 1)  # [B, T, KVH, D]
        vals = torch.cat((values["vcache"][layer], v[:, None]), 1)
        qg = q.view(batch, kvh, qh // kvh, d)
        scores = (qg @ keys.permute(0, 2, 3, 1)) * scale  # [B, KVH, G, T]
        scores = scores.masked_fill(~visible[local], -torch.inf)
        probs = F.softmax(scores, dim=-1, dtype=torch.float32).to(q.dtype)
        attn = (probs @ vals.permute(0, 2, 1, 3)).reshape(batch, qh * d)
        x = x + rms(linear("wo", layer, attn), values["ln2"][layer], gemma=True)

        xn = rms(x, values["ln3"][layer], gemma=True)
        gate = linear("wg", layer, xn)
        up = linear("wu", layer, xn)
        mlp = linear("wd", layer, F.gelu(gate, approximate="tanh") * up)
        x = x + rms(mlp, values["ln4"][layer], gemma=True)

    logits = rms(x, values["fnorm"], gemma=True) @ values["embed"].T
    return {
        "logits": logits.float(),
        "next_token": logits.argmax(-1).to(torch.int64),
        "k_write": torch.stack(k_writes).contiguous(),
        "v_write": torch.stack(v_writes).contiguous(),
    }


def _prefill_reference(case: Case, values: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    """A causal prefill chunk of one sequence on top of its cached prefix."""
    p = case.params
    h, d, context, bits, window = (p["hidden"], p["head_dim"], p["context"], p["bits"],
                                   p["local_window"])
    qh, kvh = p["q_heads"], p["kv_heads"]
    tokens = values["tokens"]
    count, device = tokens.shape[0], tokens.device
    positions = context + torch.arange(count, device=device)
    keys_at = torch.arange(context + count, device=device)[None]
    causal = keys_at <= positions[:, None]
    allowed = {False: causal, True: causal & (keys_at > positions[:, None] - window)}
    x = values["embed"][tokens] * torch.full((), math.sqrt(h), device=device, dtype=torch.bfloat16)
    scale = p.get("query_pre_attn_scalar", d) ** -0.5
    k_writes, v_writes = [], []

    def linear(name: str, layer: int, rows: torch.Tensor) -> torch.Tensor:
        return rows @ _matrix(values, name, layer, bits, rows.dtype).T

    for layer in range(p["layers"]):
        xn = rms(x, values["ln1"][layer], gemma=True)
        q = linear("wq", layer, xn).view(count, qh, d)
        k = linear("wk", layer, xn).view(count, kvh, d)
        v = linear("wv", layer, xn).view(count, kvh, d)
        q = rms(q, values["qn"][layer], gemma=True)
        k = rms(k, values["kn"][layer], gemma=True)
        local = (layer + 1) % 6 != 0
        theta = 10_000.0 if local else 1_000_000.0
        factor = 1.0 if local else 8.0
        q = rope(q, positions, theta, factor=factor)
        k = rope(k, positions, theta, factor=factor)
        k_writes.append(k.to(torch.bfloat16))
        v_writes.append(v.to(torch.bfloat16))
        keys = torch.cat((values["kcache"][layer], k), 0)
        vals = torch.cat((values["vcache"][layer], v), 0)
        attn = attention(q, keys, vals, scale=scale, allowed=allowed[local]).reshape(count, qh * d)
        x = x + rms(linear("wo", layer, attn), values["ln2"][layer], gemma=True)

        xn = rms(x, values["ln3"][layer], gemma=True)
        gate = linear("wg", layer, xn)
        up = linear("wu", layer, xn)
        mlp = linear("wd", layer, F.gelu(gate, approximate="tanh") * up)
        x = x + rms(mlp, values["ln4"][layer], gemma=True)

    logits = values["embed"] @ rms(x[-1], values["fnorm"], gemma=True)
    return {
        "logits": logits.float(),
        "next_token": logits.argmax().to(torch.int64),
        "k_write": torch.stack(k_writes).contiguous(),
        "v_write": torch.stack(v_writes).contiguous(),
    }
