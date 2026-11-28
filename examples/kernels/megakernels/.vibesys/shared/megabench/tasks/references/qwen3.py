"""Qwen3 dense decoder used only by MegaBench's PyTorch oracle."""

from __future__ import annotations

from dataclasses import dataclass

import torch

from ..common import attention, rms


@dataclass
class Config:
    hidden: int = 1024
    layers: int = 28
    q_heads: int = 16
    kv_heads: int = 8
    head_dim: int = 128
    inter: int = 3072
    vocab: int = 151936
    eps: float = 1e-6
    rope_theta: float = 1e6
    max_seq: int = 2048


def rope_tables(cfg: Config, device="cuda") -> tuple[torch.Tensor, torch.Tensor]:
    """cos/sin[max_seq, head_dim] fp32, HF layout (freqs duplicated across halves)."""
    D = cfg.head_dim
    inv_freq = cfg.rope_theta ** (-torch.arange(0, D, 2, dtype=torch.float64) / D)
    t = torch.arange(cfg.max_seq, dtype=torch.float64)
    f = torch.outer(t, inv_freq)  # [S, D/2]
    emb = torch.cat([f, f], dim=-1)  # [S, D]
    return emb.cos().float().to(device), emb.sin().float().to(device)


def _rms(x: torch.Tensor, w: torch.Tensor, eps: float) -> torch.Tensor:
    return rms(x, w, eps)


def _rot_half(x: torch.Tensor) -> torch.Tensor:
    h = x.shape[-1] // 2
    return torch.cat([-x[..., h:], x[..., :h]], dim=-1)


class RefDecoder:
    """BF16 decoder and KV cache, with FP32 normalization/attention reductions."""

    def __init__(self, cfg: Config, w: dict[str, torch.Tensor], device="cuda"):
        self.cfg = cfg
        self.w_bf16 = w
        self.cos, self.sin = rope_tables(cfg, device)
        L, S = cfg.layers, cfg.max_seq
        self.k_cache = torch.zeros(L, S, cfg.kv_heads, cfg.head_dim,
                                  device=device, dtype=torch.bfloat16)
        self.v_cache = torch.zeros_like(self.k_cache)
        self.pos = 0

    def _get(self, name, idx=None):
        t = self.w_bf16[name] if idx is None else self.w_bf16[name][idx]
        return t

    def step(self, token: int) -> torch.Tensor:
        """One decode step: returns logits[vocab] fp32; advances the KV cache."""
        cfg, g = self.cfg, self._get
        D, QH, KVH = cfg.head_dim, cfg.q_heads, cfg.kv_heads
        pos = self.pos
        x = g("embed", token).clone()  # [H]

        for l in range(cfg.layers):
            # --- attention block ---
            xn = _rms(x, g("ln1", l), cfg.eps)
            q = (g("wq", l) @ xn).view(QH, D)
            k = (g("wk", l) @ xn).view(KVH, D)
            v = (g("wv", l) @ xn).view(KVH, D)
            q = _rms(q, g("qn", l), cfg.eps)
            k = _rms(k, g("kn", l), cfg.eps)
            cos, sin = self.cos[pos], self.sin[pos]
            q = q * cos.to(q.dtype) + _rot_half(q) * sin.to(q.dtype)
            k = k * cos.to(k.dtype) + _rot_half(k) * sin.to(k.dtype)
            self.k_cache[l, pos] = k
            self.v_cache[l, pos] = v

            ks = self.k_cache[l, : pos + 1]  # [t, KVH, D]
            vs = self.v_cache[l, : pos + 1]
            o = attention(q, ks, vs).reshape(QH * D)
            x = x + g("wo", l) @ o

            # --- MLP block ---
            xn = _rms(x, g("ln2", l), cfg.eps)
            gate = g("wg", l) @ xn
            up = g("wu", l) @ xn
            x = x + g("wd", l) @ (torch.nn.functional.silu(gate) * up)

        logits = g("embed") @ _rms(x, g("fnorm"), cfg.eps)
        self.pos += 1
        return logits.float()


def batched_step(cfg: Config, w: dict[str, torch.Tensor], tokens: torch.Tensor,
                 kcache: torch.Tensor, vcache: torch.Tensor, lens: torch.Tensor,
                 cos: torch.Tensor, sin: torch.Tensor):
    """One decode step for B sequences with ragged cache lengths.

    kcache/vcache are [L, B, S, KVH, D]; sequence b attends cache positions
    below lens[b] and its own new key at position lens[b]. The caches are not
    modified. Returns logits[B, vocab] fp32 and the new k/v [L, B, KVH, D].
    """
    D, QH, KVH = cfg.head_dim, cfg.q_heads, cfg.kv_heads
    B = tokens.shape[0]
    width = kcache.shape[2]  # static, so the step stays CUDA-graph capturable
    lens = lens.to(tokens.device).long()
    visible = torch.cat([torch.arange(width, device=tokens.device)[None] < lens[:, None],
                         torch.ones(B, 1, dtype=torch.bool, device=tokens.device)], dim=1)
    visible = visible[:, None, None, :]  # [B, 1, 1, T]
    c, s = cos[lens][:, None], sin[lens][:, None]  # [B, 1, D]
    x = w["embed"][tokens]  # [B, H]
    k_new, v_new = [], []

    for l in range(cfg.layers):
        xn = _rms(x, w["ln1"][l], cfg.eps)
        q = (xn @ w["wq"][l].T).view(B, QH, D)
        k = (xn @ w["wk"][l].T).view(B, KVH, D)
        v = (xn @ w["wv"][l].T).view(B, KVH, D)
        q = _rms(q, w["qn"][l], cfg.eps)
        k = _rms(k, w["kn"][l], cfg.eps)
        q = q * c.to(q.dtype) + _rot_half(q) * s.to(q.dtype)
        k = k * c.to(k.dtype) + _rot_half(k) * s.to(k.dtype)
        k_new.append(k)
        v_new.append(v)

        keys = torch.cat([kcache[l, :, :width], k[:, None]], dim=1)  # [B, T, KVH, D]
        vals = torch.cat([vcache[l, :, :width], v[:, None]], dim=1)
        qg = q.view(B, KVH, QH // KVH, D)
        scores = (qg @ keys.permute(0, 2, 3, 1)) * D ** -0.5  # [B, KVH, G, T]
        scores = scores.masked_fill(~visible, -torch.inf)
        probs = torch.nn.functional.softmax(scores, dim=-1, dtype=torch.float32).to(q.dtype)
        o = (probs @ vals.permute(0, 2, 1, 3)).reshape(B, QH * D)
        x = x + o @ w["wo"][l].T

        xn = _rms(x, w["ln2"][l], cfg.eps)
        gate = xn @ w["wg"][l].T
        up = xn @ w["wu"][l].T
        x = x + (torch.nn.functional.silu(gate) * up) @ w["wd"][l].T

    logits = _rms(x, w["fnorm"], cfg.eps) @ w["embed"].T
    return logits.float(), torch.stack(k_new, 0), torch.stack(v_new, 0)
