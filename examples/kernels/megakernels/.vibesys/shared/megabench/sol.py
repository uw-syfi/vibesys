"""Optimistic one-read HBM traffic floors for ready MegaBench model steps.

This is a useful-byte bound, not a prediction of achievable kernel latency.
It counts each needed weight once per dependent step that must stream it,
the old KV values needed by the step, and required outputs. Weights reused
across positions that can share one pass (P0 decode, EAGLE3 verification)
count once; weights larger than L2 that sequential steps re-apply (the CSM
depth decoder, the pi0.5 action expert, the Waypoint DiT) count once per
step. It omits scratch traffic, synchronization, and instruction cost.
"""

from __future__ import annotations

import math

from .cases import Case


DEFAULT_B200_BANDWIDTH_TB_S = 8.0
DEFAULT_B200_BANDWIDTH_SOURCE = "https://www.nvidia.com/en-eu/data-center/dgx-b200/"
BF16_BYTES = 2
FP32_BYTES = 4
INT64_BYTES = 8


def estimate_case(case: Case, bandwidth_tb_s: float = DEFAULT_B200_BANDWIDTH_TB_S,
                  measured_ms: float | None = None) -> dict:
    """Return a transparent HBM floor for one ready, batch-one case."""
    if not math.isfinite(bandwidth_tb_s) or bandwidth_tb_s <= 0:
        raise ValueError("bandwidth_tb_s must be finite and positive")
    if measured_ms is not None and (not math.isfinite(measured_ms) or measured_ms <= 0):
        raise ValueError("measured_ms must be finite and positive")
    result: dict = {"case_id": case.id, "status": "estimated"}
    if not case.ready:
        return result | {"status": "unavailable", "reason": case.note}
    if work_model(case):
        return _work_floor(case, result, bandwidth_tb_s, measured_ms)
    if case.family not in {"dense_step", "moe_step", "quant_step",
                           "spec_target_step"}:
        return result | {"status": "unavailable",
                         "reason": f"no traffic model for {case.family}"}
    p = case.params
    batched = (case.family in ("dense_step", "quant_step")
               and (p["batch"] != 1 or bool(case.contexts)))
    if p["batch"] != 1 and not batched:
        return result | {"status": "unavailable",
                         "reason": "traffic model currently supports batch one"}

    layers, hidden, head_dim = p["layers"], p["hidden"], p["head_dim"]
    qdim, kdim = p["q_heads"] * head_dim, p["kv_heads"] * head_dim
    inter, vocab, context = p["intermediate"], p["vocab"], p["context"]
    steps = p["draft_depth"] + 1 if case.family == "spec_target_step" else 1
    sequences = p["batch"]
    cached = sum(case.contexts) if case.contexts else sequences * context
    projection_elements = layers * (
        qdim * hidden + 2 * kdim * hidden + hidden * qdim)
    mlp_elements = layers * 3 * inter * hidden
    components: list[dict] = []

    def add(name: str, bytes_: int, detail: str) -> None:
        components.append({"name": name, "bytes": bytes_, "detail": detail})

    if case.family == "quant_step":
        bits = p["bits"]
        if bits not in (4, 8):
            return result | {"status": "unavailable",
                             "reason": f"unsupported quantization width {bits}"}
        weight_elements = projection_elements + mlp_elements
        if weight_elements * bits % 8:
            raise ValueError(f"packed weights do not contain whole bytes: {case.id}")
        add("quantized_projection_and_mlp_weights", weight_elements * bits // 8,
            f"all seven projection stacks, packed {bits}-bit weights")
        output_rows = layers * (qdim + 2 * kdim + 2 * hidden + 2 * inter)
        add("quantization_scales", output_rows * FP32_BYTES,
            "one FP32 scale per projection output row")
        add("normalization_weights",
            (layers * (4 * hidden + 2 * head_dim) + hidden) * BF16_BYTES,
            "four layer norms, Q/K norms, and final norm in BF16")
        add("tied_embedding_and_head", vocab * hidden * BF16_BYTES,
            "full BF16 vocabulary head, including the token embedding row")
        local_layers = layers - layers // 6
        lengths = case.contexts or (context,) * sequences
        local_tokens = sum(min(n, max(0, p["local_window"] - 1)) for n in lengths)
        cache_positions = local_layers * local_tokens + (layers // 6) * sum(lengths)
    else:
        add("attention_projection_weights", projection_elements * BF16_BYTES,
            "Q/K/V/output projection weights, read once")
        cache_positions = layers * cached
        if case.family == "moe_step":
            experts, topk = p["experts"], p["topk"]
            if topk > experts:
                raise ValueError(f"topk exceeds expert count: {case.id}")
            add("router_weights", layers * experts * hidden * BF16_BYTES,
                "all router rows in each layer")
            add("active_expert_weights", mlp_elements * topk * BF16_BYTES,
                f"only the {topk} selected experts per layer")
            add("normalization_weights",
                (layers * (2 * hidden + 2 * head_dim) + hidden) * BF16_BYTES,
                "two layer norms, Q/K norms, and final norm in BF16")
            add("input_embedding_row", hidden * BF16_BYTES,
                "one BF16 embedding row")
            add("lm_head", vocab * hidden * BF16_BYTES,
                "separate BF16 vocabulary head")
        elif case.family == "dense_step":
            add("mlp_weights", mlp_elements * BF16_BYTES,
                "gate/up/down weights in every layer")
            add("normalization_weights",
                (layers * (2 * hidden + 2 * head_dim) + hidden) * BF16_BYTES,
                "two layer norms, Q/K norms, and final norm in BF16")
            add("tied_embedding_and_head", vocab * hidden * BF16_BYTES,
                "full BF16 vocabulary head, including the token embedding row")
        else:
            add("mlp_weights", mlp_elements * BF16_BYTES,
                "gate/up/down weights shared across verification positions")
            add("normalization_weights",
                (layers * 2 * hidden + hidden) * BF16_BYTES,
                "two layer norms and final norm in BF16")
            add("input_embedding_rows", steps * hidden * BF16_BYTES,
                "one BF16 embedding row per verified position")
            add("lm_head", vocab * hidden * BF16_BYTES,
                "separate BF16 vocabulary head read once across positions")

    add("old_kv_cache_reads", 2 * cache_positions * kdim * BF16_BYTES,
        "one read of the needed old K and V positions")
    add("logits_writes", sequences * steps * vocab * FP32_BYTES,
        "all required FP32 logits")
    add("new_kv_writes", 2 * layers * sequences * steps * kdim * BF16_BYTES,
        "required BF16 K and V outputs, including masked speculative slots")
    if case.family == "spec_target_step":
        depth = p["draft_depth"]
        add("token_and_feature_io",
            (steps + 3 + steps) * INT64_BYTES + 3 * hidden * BF16_BYTES,
            "input token/drafts, accepted/committed/cache scalars, "
            "committed tokens, and three target features")
    else:
        add("token_io", 2 * sequences * INT64_BYTES, "input and next-token IDs")

    minimum_bytes = sum(item["bytes"] for item in components)
    floor_ms = minimum_bytes / (bandwidth_tb_s * 1e12) * 1e3
    result.update(
        minimum_bytes=minimum_bytes,
        minimum_gb=minimum_bytes / 1e9,
        hbm_floor_ms=floor_ms,
        components=components,
    )
    if measured_ms is not None:
        result.update(measured_ms=measured_ms,
                      gap_to_hbm_floor_x=measured_ms / floor_ms,
                      percent_of_hbm_sol=100 * floor_ms / measured_ms)
    return result


# Dense tensor-core peaks per B200 (the datasheet's sparse figures halved) and
# NVLink 5 bandwidth per direction. FP8 x FP4 MMAs issue at the FP8 rate.
B200_DENSE_TFLOPS = {"bf16": 2250.0, "fp8": 4500.0}
B200_NVLINK_GB_S = 900.0
B200_PEAKS_SOURCE = "https://resources.nvidia.com/en-us-dgx-systems/dgx-b200-datasheet"


def _megamoe_work(case: Case) -> tuple[list[dict], list[dict], int]:
    """One rank of the EP routed-expert layer under uniform routing.

    Every local expert is assumed hit, which holds for the catalog's token
    counts. A token crosses NVLink once per distinct remote owner rank, both
    to dispatch FP8 rows and to return one pre-summed BF16 row per owner.
    """
    p = case.params
    tokens, experts, topk = p["tokens"], p["experts"], p["topk"]
    hidden, inter, ep = p["hidden"], p["intermediate"], case.ep
    local = experts // ep
    weight_elements = local * 3 * inter * hidden
    rows = tokens * topk
    bytes_ = [
        {"name": "local_expert_weights", "bytes": weight_elements // 2 + weight_elements // 32,
         "detail": f"{local} MXFP4 experts (E2M1 plus one E8M0 scale per 32)"},
        {"name": "fp8_tokens", "bytes": tokens * (hidden + hidden // 32),
         "detail": "E4M3 rows and per-32-channel E8M0 scales"},
        {"name": "routing", "bytes": tokens * topk * (INT64_BYTES + FP32_BYTES),
         "detail": "top-k expert ids and weights"},
        {"name": "output", "bytes": tokens * hidden * BF16_BYTES, "detail": "BF16 combined rows"},
    ]
    flops = [{"name": "expert_gemms", "dtype": "fp8", "flops": 2 * rows * 3 * inter * hidden,
              "detail": f"{rows} routed rows per rank on average, gate/up and down"}]
    # Expected distinct remote owners per token for distinct uniform experts.
    miss = math.comb(experts - local, topk) / math.comb(experts, topk)
    remote_owners = (ep - 1) * (1 - miss)
    link = round(tokens * remote_owners * (hidden + hidden // 32 + hidden * BF16_BYTES))
    return bytes_, flops, link


def _llama_layer_elements(hidden: int, qdim: int, kvdim: int, inter: int) -> int:
    return hidden * (2 * qdim + 2 * kvdim) + 3 * inter * hidden + 2 * hidden


def _csm_work(case: Case) -> tuple[list[dict], list[dict], int]:
    """One CSM frame: backbone decode, then the depth decoder's 32 positions.

    Position p >= 2 embeds the greedy code of position p - 1, so the depth
    decoder's ~0.23 GB of weights (more than the 126.5 MB B200 L2) must be
    streamed once per dependent step. Positions 0 and 1 are both known after
    the backbone and can share one two-token pass, which leaves books - 1
    reads. Each codebook head is used by one position only.
    """
    p = case.params
    hidden, books, vocab = p["hidden"], p["codebooks"], p["audio_vocab"]
    qdim, kvdim = p["q_heads"] * p["head_dim"], p["kv_heads"] * p["head_dim"]
    dhidden = p["depth_hidden"]
    dq, dkv = p["depth_q_heads"] * p["depth_head_dim"], p["depth_kv_heads"] * p["depth_head_dim"]
    backbone = p["layers"] * _llama_layer_elements(hidden, qdim, kvdim, p["intermediate"]) + hidden
    depth = (p["depth_layers"] * _llama_layer_elements(dhidden, dq, dkv, p["depth_intermediate"])
             + dhidden + dhidden * hidden)
    depth_reads = books - 1
    heads = (books - 1) * dhidden * vocab
    bytes_ = [
        {"name": "backbone_weights", "bytes": backbone * BF16_BYTES, "detail": "layers and final norm"},
        {"name": "codebook0_head", "bytes": vocab * hidden * BF16_BYTES, "detail": "backbone lm_head"},
        {"name": "depth_decoder_weights", "bytes": depth_reads * depth * BF16_BYTES,
         "detail": f"layers, final norm and input projector, read {depth_reads} times"
                   " (one per dependent step)"},
        {"name": "codebook_heads", "bytes": heads * BF16_BYTES, "detail": "one head per codebook 1..31"},
        {"name": "embedding_rows", "bytes": (2 * books - 1) * hidden * BF16_BYTES,
         "detail": "32 frame rows plus 31 depth-input rows"},
        {"name": "old_kv_cache_reads", "bytes": 2 * p["layers"] * p["context"] * kvdim * BF16_BYTES,
         "detail": "backbone K and V"},
        {"name": "outputs", "bytes": books * (INT64_BYTES + vocab * FP32_BYTES)
         + 2 * p["layers"] * kvdim * BF16_BYTES, "detail": "codes, FP32 logits and K/V writes"},
    ]
    # Position 0 of the depth decoder is a two-token prefill.
    flops = [{"name": "projections", "dtype": "bf16",
              "flops": 2 * (backbone + vocab * hidden + (books + 1) * depth + heads),
              "detail": "matrix-vector products; attention is negligible"}]
    return bytes_, flops, 0


def _waypoint_work(case: Case) -> tuple[list[dict], list[dict], int]:
    """Four denoise passes and the sigma-0 cache pass over one frame.

    The passes are sequential (each consumes the previous latent), and the
    DiT weights and visible K/V caches are far larger than L2, so both are
    streamed once per pass. The cache pass stops after the last layer's K/V
    projection: it skips that layer's Q, output and MLP weights and its
    attention reads. The sigma schedule is fixed, so the FP32 noise MLP can
    be evaluated for every pass from one read.
    """
    from .tasks.waypoint import NOISE_FOURIER_DIM, NOISE_HIDDEN_MULT, MOUSE_DIMS, SCROLL_DIMS
    from .tasks.waypoint import control_layers, visible_slots

    p = case.params
    d, layers, tokens = p["hidden"], p["layers"], p["tokens_per_frame"]
    inner, ctrl_in = d * p["mlp_ratio"], MOUSE_DIMS + p["buttons"] + SCROLL_DIMS
    qdim, kvdim = p["q_heads"] * p["head_dim"], p["kv_heads"] * p["head_dim"]
    fused = len(control_layers(case))
    patch = p["latent_channels"] * p["patch"] ** 2
    linear = 2 * qdim * d + 2 * kvdim * d + 2 * inner * d
    bf16_elements = (layers * (linear + 6 * d * d + 2 * d + 1) + fused * 3 * d * d
                     + inner * (ctrl_in + d) + 2 * d * d + 2 * d * patch + p["latent_channels"])
    noise_hidden = NOISE_HIDDEN_MULT * d
    visible = [len(visible_slots(case, layer, p["frame_index"])) for layer in range(layers)]
    latent = p["latent_channels"] * p["latent_height"] * p["latent_width"]
    passes = p["denoise_steps"]
    skipped = 2 * qdim * d + 2 * inner * d  # last layer's Q, output and MLP weights
    kv_per_slot = 2 * tokens * kvdim * BF16_BYTES
    bytes_ = [
        {"name": "dit_weights", "bytes": ((passes + 1) * bf16_elements - skipped) * BF16_BYTES,
         "detail": "blocks, AdaLN condition heads, controller fusion and patch layers,"
                   f" read by {passes} denoise passes and the cache pass"},
        {"name": "noise_mlp", "bytes": noise_hidden * (NOISE_FOURIER_DIM + d) * FP32_BYTES,
         "detail": "FP32 sigma embedding MLP"},
        {"name": "old_kv_cache_reads",
         "bytes": ((passes + 1) * sum(visible) - visible[-1]) * kv_per_slot,
         "detail": "visible local and dilated global frames, read by every pass"
                   " (the cache pass skips the last layer's)"},
        {"name": "io", "bytes": 2 * latent * BF16_BYTES + 2 * layers * tokens * kvdim * BF16_BYTES
         + ctrl_in * BF16_BYTES, "detail": "noise, controller, latent and K/V writes"},
    ]
    attention = sum(4 * tokens * (count + 1) * tokens * qdim for count in visible)
    full = 2 * tokens * (layers * linear + fused * 2 * d * d) + attention
    # The cache pass stops once the last layer's K and V exist.
    last = 2 * tokens * skipped + 4 * tokens * (visible[-1] + 1) * tokens * qdim
    flops = [{"name": "dit_passes", "dtype": "bf16", "flops": (passes + 1) * full - last,
              "detail": f"{passes} denoise passes and one cache pass over {tokens} tokens"}]
    return bytes_, flops, 0


def _pi05_work(case: Case) -> tuple[list[dict], list[dict], int]:
    """Ten flow-matching steps of the action expert over a cached prefix.

    Each step consumes the previous step's actions, and the expert's BF16
    weights are larger than L2, so they are streamed once per step. Time
    conditioning depends only on the fixed schedule, so the FP32 adaRMS and
    time-MLP weights can serve every step from one read; the small FP32
    action projections and the cached prefix K/V (18 MB) can stay in L2.
    """
    p = case.params
    w, layers, steps, horizon = p["expert_hidden"], p["layers"], p["denoise_steps"], p["horizon"]
    qdim, kvdim, act = p["q_heads"] * p["head_dim"], p["kv_heads"] * p["head_dim"], p["action_dim"]
    linear = 2 * qdim * w + 2 * kvdim * w + 3 * p["expert_intermediate"] * w
    fp32 = (2 * layers + 1) * 3 * w * (w + 1) + 2 * w * (w + 1) + 2 * act * w + w + act
    prefix = p["images"] * p["image_tokens"] + p["text_tokens"]
    bytes_ = [
        {"name": "expert_weights", "bytes": steps * layers * linear * BF16_BYTES,
         "detail": f"Gemma-300M attention and MLP projections, read once per step ({steps} steps)"},
        {"name": "fp32_conditioning_and_action_weights", "bytes": fp32 * FP32_BYTES,
         "detail": "adaRMS, time MLP and action projections"},
        {"name": "prefix_kv_reads", "bytes": 2 * layers * prefix * kvdim * BF16_BYTES,
         "detail": "cached PaliGemma prefix K and V, read once"},
        {"name": "io", "bytes": 2 * horizon * act * FP32_BYTES + prefix, "detail": "noise, mask and actions"},
    ]
    attention = 4 * horizon * (prefix + horizon) * qdim * layers
    flops = [{"name": "expert_steps", "dtype": "bf16",
              "flops": steps * (2 * horizon * layers * linear + attention),
              "detail": f"{steps} steps over {horizon} action tokens"}]
    return bytes_, flops, 0


def _quant_prefill_work(case: Case) -> tuple[list[dict], list[dict], int]:
    """One quantized Gemma 3 prefill chunk of ``tokens`` new positions.

    Weights are read once and decoded to BF16 for the tensor cores. Each
    query attends causally, and sliding-window layers see at most
    ``local_window`` positions. Only the last position computes logits.
    """
    p = case.params
    count, context, bits, window = p["tokens"], p["context"], p["bits"], p["local_window"]
    layers, hidden, head_dim = p["layers"], p["hidden"], p["head_dim"]
    qdim, kdim = p["q_heads"] * head_dim, p["kv_heads"] * head_dim
    inter, vocab = p["intermediate"], p["vocab"]
    if bits not in (4, 8):
        raise ValueError(f"unsupported quantization width {bits}: {case.id}")
    weights = layers * (2 * qdim * hidden + 2 * kdim * hidden + 3 * inter * hidden)
    local_layers, global_layers = layers - layers // 6, layers // 6
    prior = local_layers * min(context, max(0, window - 1)) + global_layers * context
    bytes_ = [
        {"name": "quantized_projection_and_mlp_weights", "bytes": weights * bits // 8,
         "detail": f"all seven projection stacks, packed {bits}-bit weights, read once"},
        {"name": "quantization_scales",
         "bytes": layers * (qdim + 2 * kdim + 2 * hidden + 2 * inter) * FP32_BYTES,
         "detail": "one FP32 scale per projection output row"},
        {"name": "normalization_weights",
         "bytes": (layers * (4 * hidden + 2 * head_dim) + hidden) * BF16_BYTES,
         "detail": "four layer norms, Q/K norms, and final norm in BF16"},
        {"name": "tied_embedding_and_head", "bytes": (vocab + count) * hidden * BF16_BYTES,
         "detail": "full BF16 vocabulary head for the last position, plus the input rows"},
        {"name": "old_kv_cache_reads", "bytes": 2 * prior * kdim * BF16_BYTES,
         "detail": "cached K and V that any new position can see, read once"},
        {"name": "new_kv_writes", "bytes": 2 * layers * count * kdim * BF16_BYTES,
         "detail": "K and V of every new position"},
    ]
    visible_global = count * context + count * (count + 1) // 2
    visible_local = sum(min(context + i + 1, window) for i in range(count))
    attention = 4 * qdim * (local_layers * visible_local + global_layers * visible_global)
    flops = [
        {"name": "projections_and_mlp", "dtype": "bf16", "flops": 2 * count * weights,
         "detail": f"{count} positions through every projection"},
        {"name": "attention", "dtype": "bf16", "flops": attention,
         "detail": "QK and PV over the causal and sliding-window masks"},
        {"name": "lm_head", "dtype": "bf16", "flops": 2 * vocab * hidden,
         "detail": "logits of the last position"},
    ]
    return bytes_, flops, 0


WORK_MODELS = {"megamoe_layer": _megamoe_work, "vla_action_step": _pi05_work, "tts_frame_step": _csm_work,
               "world_frame_step": _waypoint_work}


def work_model(case: Case):
    if case.family == "quant_step" and case.params.get("tokens", 1) > 1:
        return _quant_prefill_work
    return WORK_MODELS.get(case.family)


def _work_floor(case: Case, result: dict, bandwidth_tb_s: float,
                measured_ms: float | None) -> dict:
    """Floor = the largest of the HBM, tensor-core, and NVLink time bounds."""
    components, flops, link_bytes = work_model(case)(case)
    minimum_bytes = sum(item["bytes"] for item in components)
    hbm_ms = minimum_bytes / (bandwidth_tb_s * 1e12) * 1e3
    compute_ms = sum(item["flops"] / (B200_DENSE_TFLOPS[item["dtype"]] * 1e12)
                     for item in flops) * 1e3
    link_ms = link_bytes / (B200_NVLINK_GB_S * 1e9) * 1e3
    bounds = {"hbm": hbm_ms, "tensor_core": compute_ms, "nvlink": link_ms}
    bound = max(bounds, key=bounds.get)
    result.update(minimum_bytes=minimum_bytes, minimum_gb=minimum_bytes / 1e9,
                  hbm_floor_ms=hbm_ms, components=components,
                  tensor_flops=sum(item["flops"] for item in flops), flop_components=flops,
                  compute_floor_ms=compute_ms, nvlink_bytes=link_bytes, nvlink_floor_ms=link_ms,
                  floor_ms=bounds[bound], bound=bound, peaks_source=B200_PEAKS_SOURCE)
    if measured_ms is not None:
        result.update(measured_ms=measured_ms,
                      gap_to_hbm_floor_x=measured_ms / hbm_ms,
                      percent_of_hbm_sol=100 * hbm_ms / measured_ms,
                      gap_to_floor_x=measured_ms / bounds[bound],
                      percent_of_sol=100 * bounds[bound] / measured_ms)
    return result
