"""Qwen3 MoE decoder used only by MegaBench's PyTorch oracle."""

from __future__ import annotations

from dataclasses import dataclass

import torch

from .qwen3 import _rms, _rot_half, rope_tables
from ..common import attention


@dataclass
class MoeConfig:
    hidden: int = 2048
    layers: int = 48
    q_heads: int = 32
    kv_heads: int = 4
    head_dim: int = 128
    experts: int = 128
    topk: int = 8
    moe_inter: int = 768
    vocab: int = 151936
    eps: float = 1e-6
    rope_theta: float = 1e6
    max_seq: int = 2048

    # aliases so rope_tables()/Config-shaped helpers work unchanged
    @property
    def inter(self):
        return self.moe_inter


class MoeRefDecoder:
    """BF16 model execution with FP32 routing and expert-output reduction."""

    def __init__(self, cfg: MoeConfig, w: dict[str, torch.Tensor], device="cuda"):
        self.cfg, self.w = cfg, w
        self.cos, self.sin = rope_tables(cfg, device)
        L, S = cfg.layers, cfg.max_seq
        self.k_cache = torch.zeros(L, S, cfg.kv_heads, cfg.head_dim,
                                  device=device, dtype=torch.bfloat16)
        self.v_cache = torch.zeros_like(self.k_cache)
        self.pos = 0

    def step(self, token: int) -> torch.Tensor:
        cfg, w = self.cfg, self.w
        D, QH, KVH, I = cfg.head_dim, cfg.q_heads, cfg.kv_heads, cfg.moe_inter
        pos = self.pos
        x = w["embed"][token]

        for l in range(cfg.layers):
            xn = _rms(x, w["ln1"][l], cfg.eps)
            q = _rms((w["wq"][l] @ xn).view(QH, D), w["qn"][l], cfg.eps)
            k = _rms((w["wk"][l] @ xn).view(KVH, D), w["kn"][l], cfg.eps)
            v = (w["wv"][l] @ xn).view(KVH, D)
            cos, sin = self.cos[pos], self.sin[pos]
            q = q * cos.to(q.dtype) + _rot_half(q) * sin.to(q.dtype)
            k = k * cos.to(k.dtype) + _rot_half(k) * sin.to(k.dtype)
            self.k_cache[l, pos], self.v_cache[l, pos] = k, v

            ks, vs = self.k_cache[l, : pos + 1], self.v_cache[l, : pos + 1]
            o = attention(q, ks, vs).reshape(QH * D)
            x = x + w["wo"][l] @ o

            # --- MoE block ---
            xn = _rms(x, w["ln2"][l], cfg.eps)
            logits = w["wrt"][l].float() @ xn.float()              # FP32 router
            top_v, top_i = torch.topk(logits, cfg.topk)
            probs = torch.softmax(top_v, dim=-1)                   # == softmax-all + renorm
            acc = torch.zeros_like(x, dtype=torch.float32)
            for s in range(cfg.topk):
                e = int(top_i[s])
                gate = w["wg"][l, e] @ xn
                up = w["wu"][l, e] @ xn
                h = torch.nn.functional.silu(gate) * up
                acc = acc + (w["wd"][l, e] @ h).float() * probs[s]
            x = x + acc.to(x.dtype)

        logits = w["lm_head"] @ _rms(x, w["fnorm"], cfg.eps)
        self.pos += 1
        return logits.float()
