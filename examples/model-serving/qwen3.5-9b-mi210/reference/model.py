"""Qwen3.5 text decoder written out explicitly in PyTorch.

Semantics follow `transformers/models/qwen3_5/modeling_qwen3_5.py` (transformers
5.17), including its dtype boundaries, so the reference tracks HF numerically:

- Zero-centered RMSNorm (`x * (1 + w)`, computed in fp32) for the layer norms,
  the final norm, and the per-head q/k norms.
- Full attention: q_proj emits `[q | gate]` per head, q/k RMSNorm, partial
  rotary (first `rotary_dim` of each 256-wide head), GQA, and a sigmoid output
  gate before o_proj.
- Gated DeltaNet: fused qkv projection, depthwise causal conv (kernel 4) + SiLU,
  L2-normalized q/k, decay `g = -exp(A_log) * softplus(a + dt_bias)`, write
  strength `beta = sigmoid(b)`, fp32 delta-rule recurrence, and a gated
  RMSNorm (`norm(x) * w * silu(z)`, plain weight, not zero-centered).

Parameter names match the HF checkpoint (after the `model.language_model.`
prefix is stripped) so weights load with a strict `load_state_dict`.

The model is stateless: all per-sequence state lives in `SequenceState`, which
`forward` reads and advances. Batch dimension B is supported for sequences that
share `start_pos`; the engine currently runs B=1.
"""

from __future__ import annotations

from dataclasses import dataclass

import torch
import torch.nn.functional as F
from torch import nn

from .config import LayerType, TextConfig

# --------------------------------------------------------------------------- state


@dataclass
class AttentionCache:
    """Contiguous per-sequence KV cache for one full-attention layer."""

    k: torch.Tensor  # [B, kv_heads, capacity, head_dim]
    v: torch.Tensor


@dataclass
class DeltaNetCache:
    """Per-sequence state for one Gated DeltaNet layer."""

    conv: torch.Tensor  # [B, conv_dim, kernel - 1], pre-conv inputs of the last kernel-1 tokens
    recurrent: torch.Tensor  # [B, v_heads, k_head_dim, v_head_dim], fp32


@dataclass
class SequenceState:
    layers: list[AttentionCache | DeltaNetCache]
    capacity: int
    length: int = 0  # tokens already absorbed into the state


def new_sequence_state(
    cfg: TextConfig, batch: int, capacity: int, device: torch.device, dtype: torch.dtype
) -> SequenceState:
    layers: list[AttentionCache | DeltaNetCache] = []
    for layer_type in cfg.layer_types:
        match layer_type:
            case LayerType.FULL:
                shape = (batch, cfg.num_key_value_heads, capacity, cfg.head_dim)
                layers.append(
                    AttentionCache(
                        k=torch.empty(shape, device=device, dtype=dtype),
                        v=torch.empty(shape, device=device, dtype=dtype),
                    )
                )
            case LayerType.LINEAR:
                layers.append(
                    DeltaNetCache(
                        conv=torch.zeros(
                            batch,
                            cfg.gdn_conv_dim,
                            cfg.linear_conv_kernel_dim - 1,
                            device=device,
                            dtype=dtype,
                        ),
                        recurrent=torch.zeros(
                            batch,
                            cfg.linear_num_value_heads,
                            cfg.linear_key_head_dim,
                            cfg.linear_value_head_dim,
                            device=device,
                            dtype=torch.float32,
                        ),
                    )
                )
    return SequenceState(layers=layers, capacity=capacity)


# --------------------------------------------------------------------------- norms


class RMSNorm(nn.Module):
    """Zero-centered RMSNorm: `(x / rms(x)) * (1 + w)` in fp32, cast back to input dtype."""

    def __init__(self, dim: int, eps: float) -> None:
        super().__init__()
        self.weight = nn.Parameter(torch.zeros(dim))
        self.eps = eps

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        xf = x.float()
        xf = xf * torch.rsqrt(xf.pow(2).mean(-1, keepdim=True) + self.eps)
        return (xf * (1.0 + self.weight.float())).type_as(x)


class GatedRMSNorm(nn.Module):
    """GDN output norm: `w * norm(x)` (plain weight) then `* silu(z)`; HF's exact cast order."""

    def __init__(self, dim: int, eps: float) -> None:
        super().__init__()
        self.weight = nn.Parameter(torch.ones(dim))
        self.eps = eps

    def forward(self, x: torch.Tensor, z: torch.Tensor) -> torch.Tensor:
        dtype = x.dtype
        xf = x.float()
        xf = xf * torch.rsqrt(xf.pow(2).mean(-1, keepdim=True) + self.eps)
        out = self.weight * xf.to(dtype)
        out = out * F.silu(z.float())
        return out.to(dtype)


# --------------------------------------------------------------------------- rotary


class RotaryEmbedding(nn.Module):
    """Partial RoPE over the first `rotary_dim` channels of each head.

    The checkpoint config declares interleaved M-RoPE (sections 11/11/10). For
    text-only input all three position streams (t, h, w) are equal, so M-RoPE
    reduces exactly to 1-D RoPE with the standard `rotate_half` layout.
    """

    def __init__(self, cfg: TextConfig) -> None:
        super().__init__()
        dim = cfg.rotary_dim
        inv_freq = 1.0 / (cfg.rope_theta ** (torch.arange(0, dim, 2, dtype=torch.float32) / dim))
        self.register_buffer("inv_freq", inv_freq, persistent=False)

    def forward(
        self, positions: torch.Tensor, dtype: torch.dtype
    ) -> tuple[torch.Tensor, torch.Tensor]:
        freqs = positions.float()[:, None] * self.inv_freq.float()[None, :]  # [T, dim/2], fp32
        emb = torch.cat((freqs, freqs), dim=-1)
        return emb.cos().to(dtype), emb.sin().to(dtype)  # HF casts cos/sin to the activation dtype


def _rotate_half(x: torch.Tensor) -> torch.Tensor:
    x1, x2 = x.chunk(2, dim=-1)
    return torch.cat((-x2, x1), dim=-1)


def apply_partial_rope(x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor) -> torch.Tensor:
    """x: [B, heads, T, head_dim]; cos/sin: [T, rotary_dim]."""
    rot = cos.shape[-1]
    x_rot, x_pass = x[..., :rot], x[..., rot:]
    x_rot = x_rot * cos + _rotate_half(x_rot) * sin
    return torch.cat((x_rot, x_pass), dim=-1)


# --------------------------------------------------------------------------- full attention


class Attention(nn.Module):
    def __init__(self, cfg: TextConfig) -> None:
        super().__init__()
        self.num_heads = cfg.num_attention_heads
        self.num_kv_heads = cfg.num_key_value_heads
        self.head_dim = cfg.head_dim
        h = cfg.hidden_size
        self.q_proj = nn.Linear(
            h, self.num_heads * self.head_dim * 2, bias=False
        )  # [q | gate] per head
        self.k_proj = nn.Linear(h, self.num_kv_heads * self.head_dim, bias=False)
        self.v_proj = nn.Linear(h, self.num_kv_heads * self.head_dim, bias=False)
        self.o_proj = nn.Linear(self.num_heads * self.head_dim, h, bias=False)
        self.q_norm = RMSNorm(self.head_dim, cfg.rms_norm_eps)
        self.k_norm = RMSNorm(self.head_dim, cfg.rms_norm_eps)

    def forward(
        self,
        x: torch.Tensor,
        rope: tuple[torch.Tensor, torch.Tensor],
        cache: AttentionCache,
        start_pos: int,
    ) -> torch.Tensor:
        B, T, _ = x.shape
        q, gate = self.q_proj(x).view(B, T, self.num_heads, 2 * self.head_dim).chunk(2, dim=-1)
        gate = gate.reshape(B, T, self.num_heads * self.head_dim)
        q = self.q_norm(q).transpose(1, 2)
        k = self.k_norm(self.k_proj(x).view(B, T, self.num_kv_heads, self.head_dim)).transpose(1, 2)
        v = self.v_proj(x).view(B, T, self.num_kv_heads, self.head_dim).transpose(1, 2)
        cos, sin = rope
        q = apply_partial_rope(q, cos, sin)
        k = apply_partial_rope(k, cos, sin)

        end = start_pos + T
        cache.k[:, :, start_pos:end] = k
        cache.v[:, :, start_pos:end] = v
        keys, values = cache.k[:, :, :end], cache.v[:, :, :end]

        # Paged KV + a fused (flash / decode) attention kernel would replace this block.
        if T == 1:
            mask, causal = None, False  # one query attends to everything cached
        elif start_pos == 0:
            mask, causal = None, True
        else:  # continuation prefill over an existing prefix (chunked prefill)
            q_pos = torch.arange(start_pos, end, device=x.device)[:, None]
            mask, causal = torch.arange(end, device=x.device)[None, :] <= q_pos, False
        out = F.scaled_dot_product_attention(
            q,
            keys,
            values,
            attn_mask=mask,
            is_causal=causal,
            scale=self.head_dim**-0.5,
            enable_gqa=True,
        )
        out = out.transpose(1, 2).reshape(B, T, self.num_heads * self.head_dim)
        out = out * torch.sigmoid(gate)
        return self.o_proj(out)


# --------------------------------------------------------------------------- gated delta net


def _l2norm(x: torch.Tensor, eps: float = 1e-6) -> torch.Tensor:
    return x * torch.rsqrt((x * x).sum(dim=-1, keepdim=True) + eps)


def recurrent_gated_delta_rule(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor,
    state: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Token-by-token gated delta rule (decode path).

    q, k: [B, H, T, Dk] fp32, already L2-normalized and q scaled by Dk^-0.5.
    v: [B, H, T, Dv]; g, beta: [B, H, T]; state: [B, H, Dk, Dv] fp32.
    Per token: S = S * exp(g); S += k (beta * (v - S^T k))^T; o = S^T q.
    fla's `fused_recurrent_gated_delta_rule` computes the same thing in one kernel.
    """
    out = torch.empty_like(v)
    for t in range(q.shape[2]):
        state = state * g[:, :, t].exp()[..., None, None]
        k_t = k[:, :, t]
        kv_mem = (state * k_t[..., None]).sum(dim=-2)
        delta = (v[:, :, t] - kv_mem) * beta[:, :, t][..., None]
        state = state + k_t[..., None] * delta[..., None, :]
        out[:, :, t] = (state * q[:, :, t][..., None]).sum(dim=-2)
    return out, state


def chunked_gated_delta_rule(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor,
    state: torch.Tensor,
    chunk_size: int = 64,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Chunk-parallel (WY / UT-transform) form of the same recurrence (prefill path).

    Same contract as `recurrent_gated_delta_rule`. Mathematically identical; the
    sequential dependency is only across chunks. Mirrors HF's
    `torch_chunk_gated_delta_rule`; fla's `chunk_gated_delta_rule` is the fused
    Triton equivalent and would slot in here.
    """
    B, H, T, Dk = k.shape
    Dv = v.shape[-1]
    pad = (chunk_size - T % chunk_size) % chunk_size
    q, k, v = (F.pad(x, (0, 0, 0, pad)) for x in (q, k, v))
    g, beta = (F.pad(x, (0, pad)) for x in (g, beta))
    n = (T + pad) // chunk_size

    v_beta = v * beta[..., None]
    k_beta = k * beta[..., None]
    q, k, k_beta, v_beta = (
        x.reshape(B, H, n, chunk_size, x.shape[-1]) for x in (q, k, k_beta, v_beta)
    )
    g = g.reshape(B, H, n, chunk_size)

    upper = torch.ones(chunk_size, chunk_size, dtype=torch.bool, device=q.device).triu(1)
    g_cum = g.cumsum(dim=-1)
    # decay[i, j] = prod of exp(g) over (j, i]; zero above the diagonal.
    decay = (g_cum[..., :, None] - g_cum[..., None, :]).masked_fill(upper, float("-inf")).exp()

    ut = (
        k_beta @ k.transpose(-1, -2)
    ) * decay  # unit-lower-triangular system (diag added by the solver)
    intra = (q @ k.transpose(-1, -2)) * decay
    u = torch.linalg.solve_triangular(ut, v_beta, upper=False, unitriangular=True)
    w = torch.linalg.solve_triangular(
        ut, k_beta * g_cum.exp()[..., None], upper=False, unitriangular=True
    )

    q = q * g_cum.exp()[..., None]
    k = k * (g_cum[..., -1:] - g_cum).exp()[..., None]
    chunk_decay = g_cum[..., -1].exp()[..., None, None]

    out = torch.empty(B, H, n, chunk_size, Dv, device=v.device, dtype=v.dtype)
    for i in range(n):
        v_new = u[:, :, i] - w[:, :, i] @ state
        out[:, :, i] = q[:, :, i] @ state + intra[:, :, i] @ v_new
        state = state * chunk_decay[:, :, i] + k[:, :, i].transpose(-1, -2) @ v_new
    return out.reshape(B, H, n * chunk_size, Dv)[:, :, :T], state


class GatedDeltaNet(nn.Module):
    def __init__(self, cfg: TextConfig) -> None:
        super().__init__()
        self.num_k_heads = cfg.linear_num_key_heads
        self.num_v_heads = cfg.linear_num_value_heads
        self.head_k_dim = cfg.linear_key_head_dim
        self.head_v_dim = cfg.linear_value_head_dim
        self.key_dim = cfg.gdn_key_dim
        self.value_dim = cfg.gdn_value_dim
        self.conv_dim = cfg.gdn_conv_dim
        self.kernel = cfg.linear_conv_kernel_dim
        h = cfg.hidden_size
        self.in_proj_qkv = nn.Linear(h, self.conv_dim, bias=False)
        self.in_proj_z = nn.Linear(h, self.value_dim, bias=False)
        self.in_proj_b = nn.Linear(h, self.num_v_heads, bias=False)
        self.in_proj_a = nn.Linear(h, self.num_v_heads, bias=False)
        self.conv1d = nn.Conv1d(
            self.conv_dim, self.conv_dim, self.kernel, groups=self.conv_dim, bias=False
        )
        self.dt_bias = nn.Parameter(torch.ones(self.num_v_heads))
        self.A_log = nn.Parameter(torch.zeros(self.num_v_heads))
        self.norm = GatedRMSNorm(self.head_v_dim, cfg.rms_norm_eps)
        self.out_proj = nn.Linear(self.value_dim, h, bias=False)

    def _short_conv(self, x: torch.Tensor, cache: DeltaNetCache) -> torch.Tensor:
        """Depthwise causal conv over [cached kernel-1 inputs | new inputs]; x: [B, C, T]."""
        T = x.shape[-1]
        window = torch.cat([cache.conv, x], dim=-1)
        cache.conv.copy_(window[..., -(self.kernel - 1) :])
        # causal-conv1d (Dao-AILab) / its update kernel replaces this pair.
        out = F.conv1d(window, self.conv1d.weight, groups=self.conv_dim)
        return F.silu(out[..., -T:])

    def forward(self, x: torch.Tensor, cache: DeltaNetCache) -> torch.Tensor:
        B, T, _ = x.shape
        mixed = self._short_conv(self.in_proj_qkv(x).transpose(1, 2), cache).transpose(1, 2)
        q, k, v = torch.split(mixed, [self.key_dim, self.key_dim, self.value_dim], dim=-1)
        z = self.in_proj_z(x).view(B, T, self.num_v_heads, self.head_v_dim)
        beta = self.in_proj_b(x).sigmoid()  # [B, T, Hv], write strength
        g = -self.A_log.float().exp() * F.softplus(
            self.in_proj_a(x).float() + self.dt_bias
        )  # log decay <= 0

        # Kernel layout [B, H, T, D] in fp32. Key heads are shared by consecutive value heads.
        rep = self.num_v_heads // self.num_k_heads
        q = q.reshape(B, T, self.num_k_heads, self.head_k_dim).repeat_interleave(rep, dim=2)
        k = k.reshape(B, T, self.num_k_heads, self.head_k_dim).repeat_interleave(rep, dim=2)
        q, k, v, beta, g = (
            t.transpose(1, 2).float().contiguous()
            for t in (q, k, v.view(B, T, -1, self.head_v_dim), beta, g)
        )
        q = _l2norm(q) * self.head_k_dim**-0.5
        k = _l2norm(k)

        rule = recurrent_gated_delta_rule if T == 1 else chunked_gated_delta_rule
        out, cache.recurrent = rule(q, k, v, g, beta, cache.recurrent)

        out = out.transpose(1, 2).to(x.dtype)  # [B, T, Hv, Dv]
        out = self.norm(out, z).reshape(B, T, self.value_dim)
        return self.out_proj(out)


# --------------------------------------------------------------------------- blocks


class MLP(nn.Module):
    def __init__(self, cfg: TextConfig) -> None:
        super().__init__()
        self.gate_proj = nn.Linear(cfg.hidden_size, cfg.intermediate_size, bias=False)
        self.up_proj = nn.Linear(cfg.hidden_size, cfg.intermediate_size, bias=False)
        self.down_proj = nn.Linear(cfg.intermediate_size, cfg.hidden_size, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.down_proj(F.silu(self.gate_proj(x)) * self.up_proj(x))


class DecoderLayer(nn.Module):
    def __init__(self, cfg: TextConfig, layer_type: LayerType) -> None:
        super().__init__()
        self.layer_type = layer_type
        match layer_type:
            case LayerType.LINEAR:
                self.linear_attn = GatedDeltaNet(cfg)
            case LayerType.FULL:
                self.self_attn = Attention(cfg)
        self.mlp = MLP(cfg)
        self.input_layernorm = RMSNorm(cfg.hidden_size, cfg.rms_norm_eps)
        self.post_attention_layernorm = RMSNorm(cfg.hidden_size, cfg.rms_norm_eps)

    def forward(
        self,
        x: torch.Tensor,
        rope: tuple[torch.Tensor, torch.Tensor],
        cache: AttentionCache | DeltaNetCache,
        start_pos: int,
    ) -> torch.Tensor:
        h = self.input_layernorm(x)
        match cache:
            case DeltaNetCache():
                h = self.linear_attn(h, cache)
            case AttentionCache():
                h = self.self_attn(h, rope, cache, start_pos)
        x = x + h
        return x + self.mlp(self.post_attention_layernorm(x))


class Qwen35ForCausalLM(nn.Module):
    def __init__(self, cfg: TextConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.embed_tokens = nn.Embedding(cfg.vocab_size, cfg.hidden_size)
        self.layers = nn.ModuleList([DecoderLayer(cfg, t) for t in cfg.layer_types])
        self.norm = RMSNorm(cfg.hidden_size, cfg.rms_norm_eps)
        self.lm_head = nn.Linear(cfg.hidden_size, cfg.vocab_size, bias=False)
        self.rotary = RotaryEmbedding(cfg)

    def forward(self, input_ids: torch.Tensor, state: SequenceState) -> torch.Tensor:
        """Absorb `input_ids` [B, T] into `state`; return final-norm hidden states [B, T, hidden]."""
        T = input_ids.shape[1]
        start = state.length
        if start + T > state.capacity:
            raise ValueError(f"sequence length {start + T} exceeds state capacity {state.capacity}")
        x = self.embed_tokens(input_ids)
        rope = self.rotary(torch.arange(start, start + T, device=input_ids.device), x.dtype)
        for layer, cache in zip(self.layers, state.layers, strict=True):
            x = layer(x, rope, cache, start)
        state.length = start + T
        return self.norm(x)

    def logits(self, hidden: torch.Tensor) -> torch.Tensor:
        return self.lm_head(hidden).float()
