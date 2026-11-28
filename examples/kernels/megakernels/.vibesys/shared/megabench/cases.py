"""Whole-model inference challenges for agent-written megakernels.

Only cases with an implemented oracle are ready for evaluation. Planned cases
stay visible without entering a score.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path


BASELINE_VARIANTS = ("capture_safe_eager", "capture_safe_graph", "torch_compile",
                     "torch_compile_graph")


@dataclass(frozen=True)
class Case:
    id: str
    family: str
    model: str
    phase: str
    params: dict[str, int]
    category: str
    suite: str
    backend: str = "cuda"
    gpus: int = 1
    tp: int = 1
    ep: int = 1
    max_gpu_launches: int = 1
    atol: float = 0.0
    rtol: float = 0.0
    bf16_rtol: float = 0.008
    # Graded error may reach this multiple of the BF16 reference's own error
    # against the FP32 oracle (see docs/REFERENCE_PRECISION.md).
    noise_factor: float = 2.0
    ready: bool = False
    note: str = ""
    # "repo@revision" of real weights the inputs are read from; empty means
    # seeded synthetic weights.
    checkpoint: str = ""
    # Correctness rule: "band" bounds error by the BF16 reference's own error
    # and requires integer outputs to match; "tilert" is the per-step output
    # check of harness.correctness.tilert_metrics; "dspark" adds per-row
    # near-tie checks of speculative picks (harness.correctness.dspark_metrics).
    gate: str = "band"
    # Per-sequence KV lengths already cached (batched decode); empty means
    # every sequence holds params["context"] positions.
    contexts: tuple[int, ...] = ()
    # Weighted shapes a result is scored over; see regime_cases.
    regimes: tuple[dict, ...] = ()
    # Capture-safe baseline variants (megabench.baselines) that every timed
    # evaluation of this case runs; the fastest one counts toward the score.
    baselines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.family or not self.model or not self.phase:
            raise ValueError(f"invalid case {self.id}")
        if any(v < 1 for v in self.params.values()):
            raise ValueError(f"invalid dimensions in {self.id}")
        if min(self.gpus, self.tp, self.ep, self.max_gpu_launches) < 1:
            raise ValueError(f"invalid execution geometry in {self.id}")
        if min(self.atol, self.rtol, self.bf16_rtol) < 0 or self.noise_factor < 1:
            raise ValueError(f"invalid tolerance in {self.id}")
        if self.suite not in ("p0", "p1", "p2", "p3"):
            raise ValueError(f"invalid priority in {self.id}")
        if self.gate not in GATES:
            raise ValueError(f"invalid gate in {self.id}")
        object.__setattr__(self, "contexts", tuple(self.contexts))
        object.__setattr__(self, "baselines", tuple(self.baselines))
        if not set(self.baselines) <= set(BASELINE_VARIANTS):
            raise ValueError(f"unknown baseline variant in {self.id}")
        if self.contexts:
            if "batch" not in self.params or "context" not in self.params:
                raise ValueError(f"contexts need batch and context params in {self.id}")
            if len(self.contexts) != self.params["batch"]:
                raise ValueError(f"contexts length != batch in {self.id}")
            if any(isinstance(c, bool) or not isinstance(c, int)
                   or not 1 <= c <= self.params["context"] for c in self.contexts):
                raise ValueError(f"contexts must be ints in [1, context] in {self.id}")
        object.__setattr__(self, "regimes", tuple(_regime(r, self.id) for r in self.regimes))
        ids = [r["id"] for r in self.regimes]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate regime ids in {self.id}")
        for regime in self.regimes:
            if not set(regime["params"]) <= set(self.params):
                raise ValueError(f"regime {regime['id']} has unknown params in {self.id}")
        self.regime_cases()

    def to_dict(self) -> dict:
        # Fields at their defaults are omitted, so adding them did not change
        # the contracts recorded by earlier results.
        values = asdict(self)
        for name, default in (("checkpoint", ""), ("gate", "band"),
                              ("contexts", ()), ("regimes", ()), ("baselines", ())):
            if values[name] == default:
                del values[name]
        if "contexts" in values:
            values["contexts"] = list(values["contexts"])
        if "baselines" in values:
            values["baselines"] = list(values["baselines"])
        if "regimes" in values:
            values["regimes"] = [
                {k: list(v) if k == "contexts" else v for k, v in r.items()}
                for r in values["regimes"]]
        return values

    def regime_cases(self) -> list[tuple[dict, "Case"]]:
        return [(r, replace(self, params=self.params | r["params"],
                            contexts=tuple(r.get("contexts", ())), regimes=()))
                for r in self.regimes]


def _regime(raw: dict, case_id: str) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        raise ValueError(f"regime needs a string id in {case_id}")
    weight = raw.get("weight")
    if (isinstance(weight, bool) or not isinstance(weight, (int, float))
            or not math.isfinite(weight) or weight <= 0):
        raise ValueError(f"regime {raw['id']} needs a positive finite weight in {case_id}")
    params = dict(raw.get("params", {}))
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in params.values()):
        raise ValueError(f"regime {raw['id']} params must be ints >= 1 in {case_id}")
    out = {"id": raw["id"], "weight": float(weight), "params": params}
    if "contexts" in raw:
        out["contexts"] = tuple(raw["contexts"])
    return out


def load_regimes(path) -> tuple[dict, ...]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("regimes"), list):
        raise ValueError(f"{path}: expected {{'version': 1, 'regimes': [...]}}")
    return tuple(_regime(r, str(path)) for r in data["regimes"])


GATES = ("band", "tilert", "dspark")


# Families whose cases time one layer rather than a whole model step.
LAYER_FAMILIES = frozenset({"kimi_k3_layer", "megamoe_layer"})

DEEPSEEK_V32 = {"batch": 1, "context": 128, "layers": 61,
                "experts": 256, "topk": 8, "hidden": 7168, "q_heads": 128,
                "q_lora_rank": 1536, "kv_lora_rank": 512, "qk_nope_dim": 128,
                "qk_rope_dim": 64, "v_head_dim": 128, "intermediate": 2048,
                "dense_intermediate": 18432, "first_dense": 3, "shared_experts": 1,
                "vocab": 129280, "index_heads": 64, "index_dim": 128,
                "index_topk": 2048, "router_groups": 8, "router_top_groups": 4,
                "prompts": 4}
DEEPSEEK_CHECKPOINT = "deepseek-ai/DeepSeek-V3.2@a7e62ac04ecb2c0a54d736dc46601c5606cf10a6"

# DeepSeek-V4.1-Flash text backbone plus its three-stage DSpark drafter. The
# layer layout (sliding-window, 2:1 and 1:1 compressed layers, KV/indexer
# sharing groups, Engram and DSpark target layers) is derived from these
# counts by tasks.dsv41.layout; see model_specs for the pinned config.
DEEPSEEK_V41_FLASH = {"batch": 1, "context": 2048, "draft_depth": 5, "layers": 40,
                      "hidden": 5120, "q_heads": 64, "head_dim": 512, "rope_dim": 64,
                      "q_lora_rank": 1280, "o_lora_rank": 1024, "o_groups": 8,
                      "window": 128, "window_layers": 2, "c2_layers": 18, "c2_group": 6,
                      "c1_index_group": 4, "index_heads": 32, "index_dim": 128,
                      "index_topk": 512, "candidate_blocks": 2048, "candidate_block_size": 8,
                      "hc_mult": 4, "hc_sinkhorn_iters": 20, "experts": 384, "topk": 6,
                      "intermediate": 2304, "shared_experts": 1, "vocab": 129280,
                      "engram_first": 1, "engram_second": 14, "engram_ngram": 4,
                      "engram_heads": 8, "engram_dim": 256, "engram_buckets": 16000000,
                      "engram_compressed_vocab": 99092, "draft_layers": 3,
                      "draft_experts": 128, "draft_topk": 3, "markov_rank": 256,
                      "noise_token": 128799}
DEEPSEEK_NOTE = ("Real FP8 checkpoint MLA/DSA TP8 decode with TileRT 0.1.6 numerics (BF16 caches "
                 "and indexer) over prefilled chat prompts; TileRT-style output gate.")

# Triton W8A16 GEMMs and FlashInfer attention, compiled and graph-captured
# (megabench.baselines._gemma); the plain graph is the fallback.
GEMMA_BASELINES = ("capture_safe_graph", "torch_compile_graph")

CASES: tuple[Case, ...] = (
    Case("dense-step-qwen3-06b-b1-s128", "dense_step", "Qwen/Qwen3-0.6B",
         "decode", {"batch": 1, "context": 128, "layers": 28,
                    "hidden": 1024, "q_heads": 16, "kv_heads": 8,
                    "head_dim": 128, "intermediate": 3072, "vocab": 151936},
         "dense_llm", "p0", ready=True, atol=0.002, rtol=0.002,
         note="Full 28-layer synthetic-weight decode; checkpoint tier pending."),
    Case("moe-step-qwen3-30b-a3b-b1-s128", "moe_step",
         "Qwen/Qwen3-30B-A3B", "decode",
         {"batch": 1, "context": 128, "layers": 48, "hidden": 2048,
          "q_heads": 32, "kv_heads": 4, "head_dim": 128,
          "experts": 128, "topk": 8, "intermediate": 768, "vocab": 151936},
         "routed_moe", "p0", ready=True, atol=0.003, rtol=0.003,
         note="Full 48-layer synthetic-BF16 MoE decode; checkpoint tier pending."),
    Case("quant-step-gemma3-4b-w8-b1-s128", "quant_step",
         "google/gemma-3-4b-it", "decode",
         {"batch": 1, "context": 128, "layers": 34, "hidden": 2560,
          "q_heads": 8, "kv_heads": 4, "head_dim": 256,
          "intermediate": 10240, "vocab": 262144,
          "bits": 8, "local_window": 1024}, "quantized_llm", "p0",
         ready=True, atol=0.003, rtol=0.003,
         note="Full synthetic Gemma 3 W8A16 decode; checkpoint tier pending."),
    # Sixteen ragged sequences (KV 1K-8K, mean about 4K): still memory-bound, but
    # attention is about 40% of the traffic and the work per sequence is uneven.
    Case("quant-step-gemma3-4b-w8-b16-ragged4k", "quant_step",
         "google/gemma-3-4b-it", "decode",
         {"batch": 16, "context": 8192, "layers": 34, "hidden": 2560,
          "q_heads": 8, "kv_heads": 4, "head_dim": 256,
          "intermediate": 10240, "vocab": 262144,
          "bits": 8, "local_window": 1024}, "quantized_llm", "p1",
         ready=True, atol=0.003, rtol=0.003,
         contexts=(3488, 1093, 6421, 2290, 5059, 7905, 1530, 4207,
                   2716, 5903, 3911, 1877, 7012, 3105, 4630, 5466),
         baselines=GEMMA_BASELINES,
         note="Synthetic Gemma 3 W8A16 decode, 16 sequences with ragged KV lengths."),
    Case("quant-step-gemma3-4b-w8-prefill-t2048-c2048", "quant_step",
         "google/gemma-3-4b-it", "prefill",
         {"batch": 1, "tokens": 2048, "context": 2048, "layers": 34,
          "hidden": 2560, "q_heads": 8, "kv_heads": 4, "head_dim": 256,
          "intermediate": 10240, "vocab": 262144,
          "bits": 8, "local_window": 1024}, "quantized_llm", "p1",
         ready=True, atol=0.003, rtol=0.003, baselines=GEMMA_BASELINES,
         note="Synthetic Gemma 3 W8A16 prefill chunk: 2048 new tokens "
              "after 2048 cached tokens."),
    Case("quant-step-gemma3-4b-w4-b1-s128", "quant_step",
         "google/gemma-3-4b-it", "decode",
         {"batch": 1, "context": 128, "layers": 34, "hidden": 2560,
          "q_heads": 8, "kv_heads": 4, "head_dim": 256,
          "intermediate": 10240, "vocab": 262144,
          "bits": 4, "local_window": 1024}, "quantized_llm", "p0",
         ready=True, atol=0.003, rtol=0.003,
         note="Full synthetic Gemma 3 W4A16 decode; checkpoint tier pending."),
    Case("spec-target-step-llama31-8b-k4", "spec_target_step",
         "meta-llama/Llama-3.1-8B-Instruct + EAGLE3", "verify",
         {"batch": 1, "context": 128, "layers": 32, "draft_depth": 4,
          "hidden": 4096, "q_heads": 32, "kv_heads": 8,
          "head_dim": 128, "intermediate": 14336,
          "vocab": 128256, "draft_vocab": 32000},
         "speculative_decoding", "p0", ready=True, atol=0.003, rtol=0.003,
         note="Full synthetic Llama target verification; linear EAGLE3-head proposals."),
    Case("gptoss-step-20b-b1-s128", "gptoss_step", "openai/gpt-oss-20b",
         "decode", {"batch": 1, "context": 128, "layers": 24,
                    "experts": 32, "topk": 4, "sliding_window": 128,
                    "hidden": 2880, "q_heads": 64, "kv_heads": 8,
                    "head_dim": 64, "intermediate": 2880, "vocab": 201088},
         "routed_moe", "p1", ready=True, atol=0.003, rtol=0.003,
         note="Full synthetic native-MXFP4 decode; checkpoint tier pending."),
    Case("hybrid-step-qwen35-08b-b1-s128", "hybrid_step",
         "Qwen/Qwen3.5-0.8B", "decode",
         {"batch": 1, "context": 128, "layers": 24,
          "linear_layers": 18, "full_attention_layers": 6,
          "hidden": 1024, "intermediate": 3584, "vocab": 248320,
          "q_heads": 8, "kv_heads": 2, "head_dim": 256, "rotary_dim": 64,
          "full_attention_interval": 4, "linear_key_heads": 16,
          "linear_value_heads": 16, "linear_key_dim": 128,
          "linear_value_dim": 128, "conv_kernel": 4},
         "hybrid_llm", "p1", ready=True, atol=0.003, rtol=0.003,
         note="Full synthetic GatedDeltaNet/GQA decode, reset and continuing state."),
    Case("vl-decode-step-gemma3-4b-b1-s128", "vl_decode_step",
         "google/gemma-3-4b-it", "conditioned_decode",
         {"batch": 1, "context": 128, "layers": 34, "images": 1,
          "hidden": 2560, "intermediate": 10240, "q_heads": 8,
          "kv_heads": 4, "head_dim": 256, "vocab": 262144,
          "local_window": 1024, "image_tokens": 256, "image_size": 896,
          "patch_size": 14, "vision_hidden": 1152, "vision_layers": 27,
          "vision_intermediate": 4304, "vision_heads": 16},
         "vision_language", "p1", ready=True, atol=0.003, rtol=0.003,
         note="Synthetic SigLIP/projector/prefill fixture; timed BF16 conditioned decode."),
    Case("spec-full-iteration-llama31-8b-k4", "spec_full_iteration",
         "meta-llama/Llama-3.1-8B-Instruct + EAGLE3", "spec_iteration",
         {"batch": 1, "context": 128, "layers": 32, "draft_depth": 4,
          "hidden": 4096, "q_heads": 32, "kv_heads": 8, "head_dim": 128,
          "intermediate": 14336, "vocab": 128256, "draft_vocab": 32000,
          "tree_width": 1},
         "speculative_decoding", "p2", ready=True, atol=0.003, rtol=0.003,
         note="Full synthetic EAGLE3 draft, Llama verification, and both KV commits."),
    Case("distributed-step-gemma3-27b-tp2", "distributed_step",
         "google/gemma-3-27b-it", "decode",
         {"batch": 1, "context": 128, "layers": 62,
          "hidden": 5376, "intermediate": 21504, "q_heads": 32,
          "kv_heads": 16, "head_dim": 128, "vocab": 262144,
          "local_window": 1024, "query_pre_attn_scalar": 168},
         "tensor_parallel", "p2", ready=True, gpus=2, tp=2, atol=0.003, rtol=0.003,
         note="Full synthetic TP decode; verified on two B200 GPUs."),
    Case("distributed-step-qwen3-30b-a3b-tp2-ep2", "distributed_step",
         "Qwen/Qwen3-30B-A3B", "decode",
         {"batch": 1, "context": 128, "layers": 48,
          "experts": 128, "topk": 8, "hidden": 2048, "intermediate": 768,
          "q_heads": 32, "kv_heads": 4, "head_dim": 128, "vocab": 151936},
         "expert_parallel", "p2", ready=True, gpus=4, tp=2, ep=2, atol=0.003, rtol=0.003,
         note="Full synthetic TP/EP decode; verified on four B200 GPUs."),
    Case("deepseek-v32-step", "deepseek_v32_step",
         "deepseek-ai/DeepSeek-V3.2", "decode", DEEPSEEK_V32,
         "frontier_moe", "p3", ready=True, gpus=8, tp=8, atol=0.003, rtol=0.003,
         note=DEEPSEEK_NOTE, checkpoint=DEEPSEEK_CHECKPOINT, gate="tilert"),
    Case("glm52-step", "glm52_step", "zai-org/GLM-5.2-FP8", "decode",
         {"batch": 1, "context": 128, "layers": 78,
          "experts": 256, "topk": 8, "hidden": 6144, "q_heads": 64,
          "q_lora_rank": 2048, "kv_lora_rank": 512, "qk_nope_dim": 192,
          "qk_rope_dim": 64, "v_head_dim": 256, "intermediate": 2048,
          "dense_intermediate": 12288, "first_dense": 3, "shared_experts": 1,
          "vocab": 154880, "index_heads": 32, "index_dim": 128,
          "index_topk": 2048, "router_groups": 1, "router_top_groups": 1},
         "frontier_moe", "p3", ready=True, gpus=8, tp=8, atol=0.003, rtol=0.003,
         note="Native synthetic FP8 DSA TP8 decode; verified on eight B200 GPUs."),
    Case("glm53-flash-step", "glm53_flash_step", "zai-org/GLM-5.3-Flash", "decode",
         {"batch": 1, "context": 128, "layers": 45, "hidden": 4096,
          "hc_mult": 4, "hc_sinkhorn_iters": 20, "linear_heads": 64,
          "linear_head_dim": 128, "conv_kernel": 4, "q_heads": 64,
          "q_lora_rank": 1536, "kv_lora_rank": 512, "qk_nope_dim": 256,
          "v_head_dim": 256, "index_heads": 32, "index_dim": 128,
          "index_topk": 2048, "index_kpool": 4, "experts": 288, "topk": 8,
          "intermediate": 2048, "dense_intermediate": 12288, "first_dense": 3,
          "shared_experts": 1, "vocab": 154880},
         "frontier_hybrid", "p3", ready=True, gpus=4, tp=4, atol=0.003, rtol=0.003,
         note="Native synthetic FP8 mHC/KDA/NoPE-DSA TP4 text decode; verified on four B200 GPUs."),
    Case("kimi-k3-kda-layer-ep8", "kimi_k3_layer", "moonshotai/Kimi-K3",
         "decode", {"batch": 8, "context": 128, "layers": 93, "layer": 61,
                    "experts": 896, "topk": 16, "hidden": 7168,
                    "latent_hidden": 3584, "intermediate": 3072, "first_dense": 1,
                    "shared_experts": 2, "q_heads": 96, "head_dim": 128,
                    "q_lora_rank": 1536, "kv_lora_rank": 512,
                    "qk_nope_dim": 128, "qk_rope_dim": 64,
                    "v_head_dim": 128, "conv_kernel": 4, "attn_res_block_size": 12},
         "expert_parallel", "p3", ready=True, gpus=8, ep=8, atol=0.003, rtol=0.003,
         note="One KDA MoE layer: DP attention over 8x8 sequences, EP8 MXFP4 experts via all-to-all; verified on eight B200 GPUs."),
    Case("kimi-k3-mla-layer-ep8", "kimi_k3_layer", "moonshotai/Kimi-K3",
         "decode", {"batch": 8, "context": 128, "layers": 93, "layer": 63,
                    "experts": 896, "topk": 16, "hidden": 7168,
                    "latent_hidden": 3584, "intermediate": 3072, "first_dense": 1,
                    "shared_experts": 2, "q_heads": 96, "head_dim": 128,
                    "q_lora_rank": 1536, "kv_lora_rank": 512,
                    "qk_nope_dim": 128, "qk_rope_dim": 64,
                    "v_head_dim": 128, "conv_kernel": 4, "attn_res_block_size": 12},
         "expert_parallel", "p3", ready=True, gpus=8, ep=8, atol=0.003, rtol=0.003,
         note="One gated MLA MoE layer: DP attention over 8x8 sequences, EP8 MXFP4 experts via all-to-all; verified on eight B200 GPUs."),
    Case("kimi-k3-step", "kimi_k3_step", "moonshotai/Kimi-K3",
         "decode", {"batch": 1, "context": 128, "layers": 93,
                    "experts": 896, "topk": 16, "hidden": 7168,
                    "latent_hidden": 3584, "intermediate": 3072,
                    "dense_intermediate": 33792, "first_dense": 1,
                    "shared_experts": 2, "q_heads": 96, "head_dim": 128,
                    "q_lora_rank": 1536, "kv_lora_rank": 512,
                    "qk_nope_dim": 128, "qk_rope_dim": 64,
                    "v_head_dim": 128, "vocab": 163840,
                    "conv_kernel": 4, "attn_res_block_size": 12},
         "frontier_hybrid", "p3", gpus=16, tp=16, atol=0.003, rtol=0.003,
         note="Deferred by request; native synthetic KDA/MLA TP16 decode needs 16-GPU validation."),
    # Past index_topk=2048 cached tokens, DSA selection is sparse; these
    # contexts match TileRT's long-context decode regime.
    *(Case(f"deepseek-v32-step-ctx{context // 1024}k", "deepseek_v32_step",
           "deepseek-ai/DeepSeek-V3.2", "decode", DEEPSEEK_V32 | {"context": context},
           "frontier_moe", "p3", ready=True, gpus=8, tp=8, atol=0.003, rtol=0.003,
           note=DEEPSEEK_NOTE, checkpoint=DEEPSEEK_CHECKPOINT, gate="tilert")
      for context in (4096, 32768)),
    Case("tts-frame-step-csm-1b-b1-s128", "tts_frame_step", "sesame/csm-1b", "audio_frame",
         {"batch": 1, "context": 128, "layers": 16, "hidden": 2048, "q_heads": 32,
          "kv_heads": 8, "head_dim": 64, "intermediate": 8192, "text_vocab": 128256,
          "codebooks": 32, "audio_vocab": 2051, "depth_layers": 4, "depth_hidden": 1024,
          "depth_q_heads": 8, "depth_kv_heads": 2, "depth_head_dim": 128,
          "depth_intermediate": 8192},
         "speech_generation", "p1", ready=True, atol=0.003, rtol=0.003,
         note="Synthetic CSM backbone decode plus 31 sequential depth-decoder codebooks; verified on B200."),
    Case("vla-action-step-pi05-b1", "vla_action_step", "lerobot/pi05_base", "action_chunk",
         {"batch": 1, "images": 3, "image_tokens": 256, "image_size": 224, "patch_size": 14,
          "text_tokens": 200, "vision_hidden": 1152, "vision_layers": 27,
          "vision_intermediate": 4304, "vision_heads": 16, "layers": 18, "hidden": 2048,
          "intermediate": 16384, "q_heads": 8, "kv_heads": 1, "head_dim": 256,
          "expert_hidden": 1024, "expert_intermediate": 4096, "horizon": 50,
          "action_dim": 32, "denoise_steps": 10},
         "vision_language_action", "p2", ready=True, atol=0.003, rtol=0.003,
         note="Synthetic PaliGemma prefix fixture; timed 10-step flow-matching action expert; verified on B200."),
    Case("world-frame-step-waypoint15-1b-f128", "world_frame_step", "Overworld/Waypoint-1.5-1B",
         "latent_frame",
         {"batch": 1, "frame_index": 128, "layers": 24, "hidden": 2048, "q_heads": 32,
          "kv_heads": 16, "head_dim": 64, "mlp_ratio": 4, "latent_channels": 32,
          "latent_height": 32, "latent_width": 64, "patch": 2, "tokens_per_frame": 512,
          "local_window": 16, "global_window": 128, "global_period": 4,
          "global_dilation": 8, "buttons": 256, "control_period": 3, "denoise_steps": 4},
         "world_model", "p2", ready=True, atol=0.003, rtol=0.003,
         note="Synthetic causal DiT frame: four denoise passes plus cache commit; verified on B200."),
    # DeepGEMM/FlashInfer MegaMoE geometry: the routed experts of one
    # DeepSeek-V4-Pro MoE layer, dispatch through combine, at large batch.
    *(Case(f"megamoe-layer-deepseek-v4-pro-ep8-t{tokens}", "megamoe_layer",
           "deepseek-ai/DeepSeek-V4-Pro", "moe_layer",
           {"tokens": tokens, "experts": 384, "topk": 6, "hidden": 7168,
            "intermediate": 3072, "act_block": 32, "swiglu_limit": 10},
           "expert_parallel", "p3", ready=True, gpus=8, ep=8, atol=0.003, rtol=0.003,
           note="FP8 x MXFP4 routed-expert layer over EP8 all-to-all; verified bitwise against the serial run on eight B200 GPUs.")
      for tokens in (512, 4096)),
    # The same layer's work for one GPU without communication: 48 experts (one EP8
    # rank's share; expert e has the EP8 cases' weights for expert e) and T tokens
    # routed top-6 among them, so every expert sees T/8 rows on average, as an EP8
    # rank does. Route, linear1, SwiGLU, FP8 requantization, linear2 and combine.
    *(Case(f"megamoe-layer-deepseek-v4-pro-1gpu-e48-t{tokens}", "megamoe_layer",
           "deepseek-ai/DeepSeek-V4-Pro", "moe_layer",
           {"tokens": tokens, "experts": 48, "topk": 6, "hidden": 7168,
            "intermediate": 3072, "act_block": 32, "swiglu_limit": 10},
           "routed_moe", "p3", ready=True, atol=0.003, rtol=0.003,
           note="Single-GPU FP8 x MXFP4 routed-expert layer: one EP8 rank's expert work without "
                "dispatch/combine communication.")
      for tokens in (512, 4096)),
    # One DSpark iteration: verify the committed token plus 5 proposals (6 rows)
    # against 2,048 cached tokens, accept the greedy prefix, draft 5 more.
    Case("dsv41-flash-dspark-step-k5-ctx2k", "dsv41_dspark_step",
         "deepseek-ai/DeepSeek-V4.1-Flash + DSpark", "spec_iteration", DEEPSEEK_V41_FLASH,
         "speculative_decoding", "p3", ready=True, gpus=8, tp=8, atol=0.003, rtol=0.003,
         gate="dspark",
         note="Native synthetic FP8/FP4 TP8 DSpark iteration: 6-row CSA/indexer verify, prefix "
              "acceptance and 3-stage DSpark draft at B=1, context 2048."),
)


# Trainium2 (trn2.3xlarge): each P0 case on the whole chip, as four ranks on
# its logical NeuronCores (LNC=2, 24 GiB each). Inputs are split as in
# tasks/sharding.py and graded against the unchanged single-device reference.
TRN2_TP4_NOTE = ("Whole Trainium2 chip as four TP ranks (one per logical NeuronCore); "
                 "graded against the single-device reference.")
# The P0 MoE case is included although its synthetic 48-layer stack is chaotic (the BF16
# reference is about 100% off the FP32 oracle on logits at full depth), so its oracle band is
# wide; the CUDA case is graded with the same gate.
CASES += tuple(replace(case, id=f"{case.id}-trn2-tp4", backend="neuron", gpus=4, tp=4,
                       note=TRN2_TP4_NOTE)
               for case in CASES if case.suite == "p0" and case.ready)
# The multimodal cases that run on one B200 in the CUDA campaign run on one logical
# NeuronCore (24 GiB): one rank, unsharded inputs, the same single-device contract.
TRN2_NC1_CASES = ("tts-frame-step-csm-1b-b1-s128", "vla-action-step-pi05-b1",
                  "world-frame-step-waypoint15-1b-f128")
TRN2_NC1_NOTE = ("One logical NeuronCore of a Trainium2 chip (LNC=2, 24 GiB), one rank; "
                 "graded against the single-device reference.")
CASES += tuple(replace(case, id=f"{case.id}-trn2-nc1", backend="neuron", gpus=1, tp=1,
                       note=TRN2_NC1_NOTE)
               for case in CASES if case.id in TRN2_NC1_CASES)

# "cuda" covers NVIDIA and ROCm devices; "neuron" covers AWS Trainium.
BACKENDS = ("cuda", "neuron")


def select_cases(suite: str = "core", ids: list[str] | None = None,
                 backend: str = "cuda") -> list[Case]:
    """Cases by id, or a suite's cases for one backend."""
    by_id = {case.id: case for case in CASES}
    if ids:
        missing = sorted(set(ids) - by_id.keys())
        if missing:
            raise ValueError(f"unknown cases: {', '.join(missing)}")
        return [by_id[id] for id in ids]
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend}")
    cases = [case for case in CASES if case.backend == backend]
    if suite == "all":
        return cases
    if suite == "core":
        return [case for case in cases if case.ready]
    if suite == "planned":
        return [case for case in cases if not case.ready]
    if suite in ("p0", "p1", "p2", "p3"):
        return [case for case in cases if case.suite == suite]
    raise ValueError(f"unknown suite {suite}")
