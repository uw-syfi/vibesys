"""Full Qwen3-30B-A3B synthetic-weight MoE decode fixture and oracle."""

from __future__ import annotations

import torch

from .references.moe import MoeConfig, MoeRefDecoder

from ..cases import Case
from .common import fill_layer_weights, random_norm, random_weight


WEIGHT_NAMES = (
    "ln1", "ln2", "fnorm", "wq", "wk", "wv", "wo", "qn", "kn",
    "wrt", "wg", "wu", "wd", "embed", "lm_head",
)


def config(case: Case) -> MoeConfig:
    p = case.params
    return MoeConfig(
        hidden=p["hidden"], layers=p["layers"], q_heads=p["q_heads"],
        kv_heads=p["kv_heads"], head_dim=p["head_dim"],
        experts=p["experts"], topk=p["topk"], moe_inter=p["intermediate"],
        vocab=p["vocab"], max_seq=p["context"] + 1,
    )


def make_inputs(case: Case, seed: int, device: str) -> dict[str, torch.Tensor]:
    p, cfg = case.params, config(case)
    if p["batch"] != 1:
        raise NotImplementedError("Qwen3 MoE fixture currently handles batch one")
    g = torch.Generator(device=device).manual_seed(seed)
    h, layers, inter, d, experts = (cfg.hidden, cfg.layers, cfg.moe_inter,
                                    cfg.head_dim, cfg.experts)
    qdim, kdim = cfg.q_heads * d, cfg.kv_heads * d
    out = {
        "token": torch.randint(cfg.vocab, (), generator=g, device=device,
                               dtype=torch.int64),
        "ln1": random_norm(g, device, (layers, h)),
        "ln2": random_norm(g, device, (layers, h)),
        "fnorm": random_norm(g, device, (h,)),
        "wq": fill_layer_weights(g, device, (layers, qdim, h), h),
        "wk": fill_layer_weights(g, device, (layers, kdim, h), h),
        "wv": fill_layer_weights(g, device, (layers, kdim, h), h),
        "wo": fill_layer_weights(g, device, (layers, h, qdim), qdim),
        "qn": random_norm(g, device, (layers, d)),
        "kn": random_norm(g, device, (layers, d)),
        "wrt": fill_layer_weights(g, device, (layers, experts, h), h),
        "wg": fill_layer_weights(g, device, (layers, experts, inter, h), h),
        "wu": fill_layer_weights(g, device, (layers, experts, inter, h), h),
        "wd": fill_layer_weights(g, device, (layers, experts, h, inter), inter),
        "embed": random_weight(g, device, (cfg.vocab, h), h),
        "lm_head": random_weight(g, device, (cfg.vocab, h), h),
        "kcache": random_weight(g, device, (layers, p["context"], cfg.kv_heads, d), d),
        "vcache": random_weight(g, device, (layers, p["context"], cfg.kv_heads, d), d),
    }
    return out


def reference(case: Case, values: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    cfg = config(case)
    ref = MoeRefDecoder(cfg, {name: values[name] for name in WEIGHT_NAMES},
                        device=str(values["token"].device))
    context = case.params["context"]
    ref.k_cache[:, :context] = values["kcache"]
    ref.v_cache[:, :context] = values["vcache"]
    ref.pos = context
    logits = ref.step(int(values["token"].item()))
    return {
        "logits": logits.float(),
        "next_token": logits.argmax().to(torch.int64),
        "k_write": ref.k_cache[:, context].contiguous(),
        "v_write": ref.v_cache[:, context].contiguous(),
    }
