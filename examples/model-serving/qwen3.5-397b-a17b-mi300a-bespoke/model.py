"""Deliberately slow, plain-PyTorch Qwen3.5-MoE (text only), written from the HF modeling code.

Design (see reference/modeling_qwen3_5_moe.py for the semantics this mirrors):
- batch size 1, one sequence at a time, static caches allocated once.
- full-attention layers: static KV cache [1, kv_heads, max_seq, head_dim].
- Gated DeltaNet layers: static conv state [1, conv_dim, K-1] and fp32 recurrent state
  [1, v_heads, k_dim, v_dim]; the delta rule runs as a per-token python loop.
- layers are split contiguously over the given devices (one process, activations hop devices).
- routed experts stay MXFP4 in memory and are dequantized per expert on use.

Memory math for the real model (60 layers x 512 experts x 3 x 1024x4096 = 386.5 B params):
  bf16 experts = 773 GB > 4 x 128 GB = 512 GB, so they cannot be held dense.
  MXFP4 experts = 386.5 B x (4 + 8/32) bits = ~205 GB, plus ~20 GB bf16 for everything else.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import torch
import torch.nn.functional as F
from mxfp4 import dequant_mxfp4
from weights import Checkpoint

PREFILL_CHUNK = 512
PREFIX = "model.language_model."


@dataclass(frozen=True)
class Cfg:
    hidden: int
    vocab: int
    layer_types: tuple[str, ...]
    eps: float
    # full attention
    heads: int
    kv_heads: int
    head_dim: int
    rot_dim: int
    rope_theta: float
    # gated deltanet
    k_heads: int
    v_heads: int
    k_dim: int
    v_dim: int
    conv_k: int
    # moe
    experts: int
    top_k: int
    eos: tuple[int, ...]


def load_cfg(model_dir: str | Path) -> Cfg:
    raw = json.loads((Path(model_dir) / "config.json").read_text())
    t = raw.get("text_config", raw)
    rope = t.get("rope_parameters", {})
    head_dim = t["head_dim"]
    eos = t.get("eos_token_id", ())
    return Cfg(
        hidden=t["hidden_size"],
        vocab=t["vocab_size"],
        layer_types=tuple(t["layer_types"]),
        eps=t["rms_norm_eps"],
        heads=t["num_attention_heads"],
        kv_heads=t["num_key_value_heads"],
        head_dim=head_dim,
        rot_dim=int(
            head_dim * rope.get("partial_rotary_factor", t.get("partial_rotary_factor", 1.0))
        ),
        rope_theta=rope.get("rope_theta", t.get("rope_theta", 1e7)),
        k_heads=t["linear_num_key_heads"],
        v_heads=t["linear_num_value_heads"],
        k_dim=t["linear_key_head_dim"],
        v_dim=t["linear_value_head_dim"],
        conv_k=t["linear_conv_kernel_dim"],
        experts=t["num_experts"],
        top_k=t["num_experts_per_tok"],
        eos=(eos,) if isinstance(eos, int) else tuple(eos),
    )


# ---------------------------------------------------------------- small math helpers


def rmsnorm(x: torch.Tensor, w: torch.Tensor, eps: float) -> torch.Tensor:
    """Qwen3.5 RMSNorm: normalize in fp32, scale by (1 + w)."""
    y = x.float() * torch.rsqrt(x.float().pow(2).mean(-1, keepdim=True) + eps)
    return (y * (1.0 + w.float())).type_as(x)


def gated_rmsnorm(x: torch.Tensor, gate: torch.Tensor, w: torch.Tensor, eps: float) -> torch.Tensor:
    """DeltaNet output norm: plain weight (no +1), then silu(gate)."""
    y = x.float() * torch.rsqrt(x.float().pow(2).mean(-1, keepdim=True) + eps)
    y = w * y.to(x.dtype)
    return (y * F.silu(gate.float())).to(x.dtype)


def l2norm(x: torch.Tensor, eps: float = 1e-6) -> torch.Tensor:
    return x * torch.rsqrt((x * x).sum(-1, keepdim=True) + eps)


def rotate_half(x: torch.Tensor) -> torch.Tensor:
    h = x.shape[-1] // 2
    return torch.cat((-x[..., h:], x[..., :h]), dim=-1)


def apply_rope(x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor) -> torch.Tensor:
    """Rotate the first rot_dim channels of x [1, heads, T, head_dim]; pass the rest through."""
    r = cos.shape[-1]
    rot, rest = x[..., :r], x[..., r:]
    return torch.cat([rot * cos + rotate_half(rot) * sin, rest], dim=-1)


def swiglu_mlp(
    x: torch.Tensor, gate: torch.Tensor, up: torch.Tensor, down: torch.Tensor
) -> torch.Tensor:
    return F.linear(F.silu(F.linear(x, gate)) * F.linear(x, up), down)


# ---------------------------------------------------------------- weight loading


def load_experts(ck: Checkpoint, p: str, cfg: Cfg, dev: torch.device, dtype: torch.dtype) -> dict:
    """Stack per-expert tensors into [E, ...] tensors. MXFP4 checkpoints keep uint8 + scale."""
    quant = ck.has(f"{p}.experts.0.gate_proj.weight_scale")

    def stack(read) -> torch.Tensor:
        first = read(0)
        out = torch.empty((cfg.experts, *first.shape), dtype=first.dtype, device=dev)
        out[0] = first
        for e in range(1, cfg.experts):
            out[e] = read(e)
        return out

    keep = (
        None if quant else dtype
    )  # uint8 payloads stay uint8; dense checkpoints cast to model dtype

    def ld(e: int, proj: str, suffix: str = "weight") -> torch.Tensor:
        return ck.load(
            f"{p}.experts.{e}.{proj}.{suffix}", dev, keep if suffix == "weight" else None
        )

    ex = {
        "gate_up": stack(lambda e: torch.cat([ld(e, "gate_proj"), ld(e, "up_proj")], 0)),
        "down": stack(lambda e: ld(e, "down_proj")),
    }
    if quant:
        ex["gate_up_scale"] = stack(
            lambda e: torch.cat(
                [ld(e, "gate_proj", "weight_scale"), ld(e, "up_proj", "weight_scale")], 0
            )
        )
        ex["down_scale"] = stack(lambda e: ld(e, "down_proj", "weight_scale"))
    return ex


def load_layer(ck: Checkpoint, i: int, cfg: Cfg, dev: torch.device, dtype: torch.dtype) -> dict:
    p = f"{PREFIX}layers.{i}"

    def w(name: str, dt: torch.dtype | None = dtype) -> torch.Tensor:
        return ck.load(f"{p}.{name}", dev, dt)

    layer = {
        "in_norm": w("input_layernorm.weight"),
        "post_norm": w("post_attention_layernorm.weight"),
    }
    if cfg.layer_types[i] == "full_attention":
        for n in ("q_proj", "k_proj", "v_proj", "o_proj"):
            layer[n] = w(f"self_attn.{n}.weight")
        layer["q_norm"], layer["k_norm"] = (
            w("self_attn.q_norm.weight"),
            w("self_attn.k_norm.weight"),
        )
    else:
        for n in ("in_proj_qkv", "in_proj_z", "in_proj_a", "in_proj_b", "out_proj"):
            layer[n] = w(f"linear_attn.{n}.weight")
        layer["conv"] = w("linear_attn.conv1d.weight")
        layer["A_log"], layer["dt_bias"] = (
            w("linear_attn.A_log", torch.float32),
            w("linear_attn.dt_bias", torch.float32),
        )
        layer["dn_norm"] = w("linear_attn.norm.weight")
    for n in ("shared_expert.gate_proj", "shared_expert.up_proj", "shared_expert.down_proj"):
        layer[n] = w(f"mlp.{n}.weight")
    layer["router"], layer["shared_gate"] = w("mlp.gate.weight"), w("mlp.shared_expert_gate.weight")
    layer["experts"] = load_experts(ck, f"{p}.mlp", cfg, dev, dtype)
    return layer


# ---------------------------------------------------------------- the model


class Model:
    def __init__(
        self, model_dir: str | Path, devices: list[str], dtype: torch.dtype, max_seq: int
    ) -> None:
        self.cfg = cfg = load_cfg(model_dir)
        self.dtype, self.max_seq = dtype, max_seq
        self.devices = [torch.device(d) for d in devices]
        n = len(cfg.layer_types)
        self.layer_dev = [
            self.devices[i * len(self.devices) // n] for i in range(n)
        ]  # contiguous split
        ck = Checkpoint(model_dir)
        self.embed = ck.load(f"{PREFIX}embed_tokens.weight", self.devices[0], dtype)
        self.layers = [load_layer(ck, i, cfg, self.layer_dev[i], dtype) for i in range(n)]
        last = self.devices[-1]
        self.final_norm = ck.load(f"{PREFIX}norm.weight", last, dtype)
        self.lm_head = (
            ck.load("lm_head.weight", last, dtype)
            if ck.has("lm_head.weight")
            else self.embed.to(last)
        )
        self.state = [self._new_state(i) for i in range(n)]

    # -- static state ---------------------------------------------------------
    def _new_state(self, i: int) -> dict:
        c, dev = self.cfg, self.layer_dev[i]
        if c.layer_types[i] == "full_attention":
            shape = (1, c.kv_heads, self.max_seq, c.head_dim)
            return {
                "k": torch.zeros(shape, dtype=self.dtype, device=dev),
                "v": torch.zeros(shape, dtype=self.dtype, device=dev),
            }
        conv_dim = 2 * c.k_heads * c.k_dim + c.v_heads * c.v_dim
        return {
            "conv": torch.zeros(1, conv_dim, c.conv_k - 1, dtype=self.dtype, device=dev),
            "rec": torch.zeros(1, c.v_heads, c.k_dim, c.v_dim, dtype=torch.float32, device=dev),
        }

    def reset(self) -> None:
        """Start a new sequence. KV entries are masked by length, so only DeltaNet state needs zeroing."""
        for st in self.state:
            if "conv" in st:
                st["conv"].zero_()
                st["rec"].zero_()

    # -- layers ---------------------------------------------------------------
    def full_attention(self, i: int, x: torch.Tensor, start: int) -> torch.Tensor:
        c, w, st = self.cfg, self.layers[i], self.state[i]
        t = x.shape[1]
        q, gate = F.linear(x, w["q_proj"]).view(1, t, c.heads, 2 * c.head_dim).chunk(2, dim=-1)
        q = rmsnorm(q, w["q_norm"], c.eps).transpose(1, 2)
        k = rmsnorm(
            F.linear(x, w["k_proj"]).view(1, t, c.kv_heads, c.head_dim), w["k_norm"], c.eps
        ).transpose(1, 2)
        v = F.linear(x, w["v_proj"]).view(1, t, c.kv_heads, c.head_dim).transpose(1, 2)
        cos, sin = self.rope(start, t, x)
        q, k = apply_rope(q, cos, sin), apply_rope(k, cos, sin)
        st["k"][:, :, start : start + t] = k
        st["v"][:, :, start : start + t] = v
        rep = c.heads // c.kv_heads
        keys = st["k"][:, :, : start + t].repeat_interleave(rep, dim=1)
        vals = st["v"][:, :, : start + t].repeat_interleave(rep, dim=1)
        scores = (q @ keys.transpose(2, 3)) * c.head_dim**-0.5
        causal = (
            torch.arange(start + t, device=x.device)[None, :]
            <= (start + torch.arange(t, device=x.device))[:, None]
        )
        scores = scores.masked_fill(~causal, float("-inf"))
        out = (scores.float().softmax(-1).to(x.dtype) @ vals).transpose(1, 2).reshape(1, t, -1)
        return F.linear(out * torch.sigmoid(gate.reshape(1, t, -1)), w["o_proj"])

    def rope(self, start: int, t: int, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        c = self.cfg
        inv = 1.0 / (
            c.rope_theta ** (torch.arange(0, c.rot_dim, 2, device=x.device).float() / c.rot_dim)
        )
        pos = torch.arange(start, start + t, device=x.device).float()
        emb = torch.cat([pos[:, None] * inv[None], pos[:, None] * inv[None]], dim=-1)
        return emb.cos().to(x.dtype), emb.sin().to(x.dtype)

    def deltanet(self, i: int, x: torch.Tensor) -> torch.Tensor:
        c, w, st = self.cfg, self.layers[i], self.state[i]
        t = x.shape[1]
        key_dim, val_dim = c.k_heads * c.k_dim, c.v_heads * c.v_dim
        z = F.linear(x, w["in_proj_z"]).view(1, t, c.v_heads, c.v_dim)
        mixed = self.causal_conv(
            F.linear(x, w["in_proj_qkv"]).transpose(1, 2), w["conv"], st["conv"]
        )
        q, k, v = mixed.transpose(1, 2).split([key_dim, key_dim, val_dim], dim=-1)
        q, k = q.reshape(1, t, c.k_heads, c.k_dim), k.reshape(1, t, c.k_heads, c.k_dim)
        v = v.reshape(1, t, c.v_heads, c.v_dim)
        beta = F.linear(x, w["in_proj_b"]).sigmoid()
        g = -w["A_log"].exp() * F.softplus(F.linear(x, w["in_proj_a"]).float() + w["dt_bias"])
        rep = c.v_heads // c.k_heads
        q, k = q.repeat_interleave(rep, dim=2), k.repeat_interleave(rep, dim=2)
        out = self.delta_rule(q, k, v, g, beta, st["rec"]).to(x.dtype)
        out = gated_rmsnorm(out.reshape(-1, c.v_dim), z.reshape(-1, c.v_dim), w["dn_norm"], c.eps)
        return F.linear(out.reshape(1, t, -1), w["out_proj"])

    @staticmethod
    def causal_conv(x: torch.Tensor, weight: torch.Tensor, state: torch.Tensor) -> torch.Tensor:
        """Depthwise causal conv over [1, C, T] with the previous K-1 inputs in `state` (updated in place)."""
        full = torch.cat([state, x], dim=-1)
        state.copy_(full[:, :, -state.shape[-1] :])
        return F.silu(F.conv1d(full, weight, groups=full.shape[1]))

    @staticmethod
    def delta_rule(q, k, v, g, beta, rec) -> torch.Tensor:  # noqa: ANN001
        """Recurrent gated delta rule, one token at a time, fp32. q,k,v: [1,T,H,d]; g,beta: [1,T,H]."""
        q, k, v, beta = (
            (l2norm(q.float()) * q.shape[-1] ** -0.5),
            l2norm(k.float()),
            v.float(),
            beta.float(),
        )
        out = torch.empty_like(v)
        for s in range(q.shape[1]):
            rec.mul_(g[:, s].exp()[..., None, None])
            mem = (rec * k[:, s, :, :, None]).sum(-2)
            delta = (v[:, s] - mem) * beta[:, s, :, None]
            rec.add_(k[:, s, :, :, None] * delta[:, :, None, :])
            out[:, s] = (rec * q[:, s, :, :, None]).sum(-2)
        return out

    # -- MoE ------------------------------------------------------------------
    def expert_weights(self, ex: dict, e: int) -> tuple[torch.Tensor, torch.Tensor]:
        """Dense (gate_up, down) for expert e, dequantized on the fly if stored as MXFP4."""
        if "gate_up_scale" not in ex:
            return ex["gate_up"][e], ex["down"][e]
        return (
            dequant_mxfp4(ex["gate_up"][e], ex["gate_up_scale"][e], self.dtype),
            dequant_mxfp4(ex["down"][e], ex["down_scale"][e], self.dtype),
        )

    def moe(self, i: int, x: torch.Tensor) -> torch.Tensor:
        c, w = self.cfg, self.layers[i]
        h = x.reshape(-1, c.hidden)
        probs = F.linear(h, w["router"]).softmax(-1, dtype=torch.float)
        top_w, top_i = probs.topk(c.top_k, dim=-1)
        top_w = (top_w / top_w.sum(-1, keepdim=True)).to(h.dtype)
        out = torch.zeros_like(h)
        for e in top_i.unique().tolist():
            tok, slot = (top_i == e).nonzero(as_tuple=True)
            gate_up, down = self.expert_weights(w["experts"], e)
            gate, up = F.linear(h[tok], gate_up).chunk(2, dim=-1)
            y = F.linear(F.silu(gate) * up, down) * top_w[tok, slot, None]
            out.index_add_(0, tok, y.to(out.dtype))
        shared = swiglu_mlp(
            h,
            w["shared_expert.gate_proj"],
            w["shared_expert.up_proj"],
            w["shared_expert.down_proj"],
        )
        out = out + torch.sigmoid(F.linear(h, w["shared_gate"])) * shared
        return out.reshape(x.shape)

    def layer(self, i: int, x: torch.Tensor, start: int) -> torch.Tensor:
        w, c = self.layers[i], self.cfg
        h = rmsnorm(x, w["in_norm"], c.eps)
        mixer = (
            self.full_attention(i, h, start)
            if c.layer_types[i] == "full_attention"
            else self.deltanet(i, h)
        )
        x = x + mixer
        return x + self.moe(i, rmsnorm(x, w["post_norm"], c.eps))

    # -- entry points ---------------------------------------------------------
    @torch.no_grad()
    def forward(self, ids: torch.Tensor, start: int, *, all_logits: bool = False) -> torch.Tensor:
        """Run tokens ids [1, T] at positions start..start+T. Returns fp32 logits [T or 1, vocab]."""
        x = F.embedding(ids.to(self.devices[0]), self.embed)
        for i in range(len(self.layers)):
            x = self.layer(i, x.to(self.layer_dev[i]), start)
        x = x.to(self.devices[-1]) if all_logits else x[:, -1:].to(self.devices[-1])
        return F.linear(rmsnorm(x, self.final_norm, self.cfg.eps), self.lm_head)[0].float()

    @torch.no_grad()
    def generate(self, prompt: list[int], max_new: int, temperature: float, stop: frozenset[int]):
        """Yield generated token ids (a stop token is yielded last, then generation ends)."""
        self.reset()
        logits = None
        for s in range(0, len(prompt), PREFILL_CHUNK):
            chunk = torch.tensor([prompt[s : s + PREFILL_CHUNK]])
            logits = self.forward(chunk, s)
        pos = len(prompt)
        for _ in range(max_new):
            tok = self.sample(logits[-1], temperature)
            yield tok
            if tok in stop:
                return
            logits = self.forward(torch.tensor([[tok]]), pos)
            pos += 1

    @staticmethod
    def sample(logits: torch.Tensor, temperature: float) -> int:
        if temperature <= 0:
            return int(logits.argmax())
        return int(torch.multinomial((logits / temperature).softmax(-1), 1))

    def warmup(self) -> None:
        for _ in self.generate([1, 2, 3, 4], 2, 0.0, frozenset()):
            pass
        self.reset()
