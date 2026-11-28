"""Validate submission outputs against a task reference."""

from __future__ import annotations

import secrets
import time
from typing import Callable

from ..cases import Case
from ..heartbeat import beat
from ..tasks.workloads import make_inputs, oracle, reference, regrade, set_trial_scenario


_PRINT_CHUNK = 1 << 24  # words per step: bounds the device memory of a fingerprint


def input_fingerprints(inputs: dict, key: int | None = None) -> tuple[int, dict]:
    """Bit-exact fingerprints of *inputs*, computed where each tensor lives.

    A copy of every input on the host costs as much host memory as the inputs (more than
    60 GB for full MoE weights), so the mutation check keys a hash of the raw bytes
    instead. Each word w at position i adds (w + i*a) * b and ((w * c) ^ (i * d)) to two
    64-bit sums with random odd keys: any change of one word changes both sums, and the
    keys are new for each check, so changes that cancel are improbable.
    """
    import torch
    key = secrets.randbits(63) if key is None else key
    rng = torch.Generator().manual_seed(key)
    a, b, c, d = (int(x) | 1 for x in torch.randint(1, 2**62, (4,), generator=rng))
    prints = {}
    for name, value in inputs.items():
        if not isinstance(value, torch.Tensor):
            prints[name] = ("value", repr(value))
            continue
        t = value.detach()
        meta = (tuple(t.shape), str(t.dtype), tuple(t.stride()), t.storage_offset())
        if t.numel() == 0:
            prints[name] = (meta, 0, 0)
            continue
        raw = t.contiguous().view(-1).view(torch.uint8)
        try:
            words = raw.view(torch.int32) if raw.numel() % 4 == 0 else raw
        except RuntimeError:  # storage offset not aligned to 4 bytes
            words = raw
        h1 = torch.zeros((), dtype=torch.int64, device=t.device)
        h2 = torch.zeros((), dtype=torch.int64, device=t.device)
        for start in range(0, words.numel(), _PRINT_CHUNK):
            w = words[start:start + _PRINT_CHUNK].to(torch.int64)
            i = torch.arange(start, start + w.numel(), dtype=torch.int64, device=t.device)
            h1 += ((w + i * a) * b).sum()
            h2 += ((w * c) ^ (i * d)).sum()
            del w, i
        prints[name] = (meta, int(h1), int(h2))
    return key, prints


def mutated_inputs(inputs: dict, before: tuple[int, dict]) -> list[str]:
    """Names of the inputs whose fingerprint differs from *before* (input_fingerprints)."""
    key, prints = before
    _, now = input_fingerprints(inputs, key)
    return [name for name in prints if now.get(name) != prints[name]]


def _band(want, got, exact, case: Case) -> dict:
    """Bound candidate error by a multiple of the BF16 reference's own error.

    Both errors are measured against the FP32 oracle. The relative L2 bound
    covers distributed drift; the peak bound catches localized corruption.
    """
    import torch

    exact = exact.double()
    error, ref_error = got.double() - exact, want.double() - exact
    scale = exact.norm().clamp_min(torch.finfo(torch.float64).tiny)
    rtol = case.bf16_rtol if want.dtype == torch.bfloat16 else case.rtol
    k = case.noise_factor
    detail = {"relative_error": float(error.norm() / scale),
              "reference_relative_error": float(ref_error.norm() / scale),
              "max_abs_error": float(error.abs().max()),
              "reference_max_abs_error": float(ref_error.abs().max())}
    detail["pass"] = bool(
        detail["relative_error"] <= k * detail["reference_relative_error"] + rtol and
        detail["max_abs_error"] <= (k * detail["reference_max_abs_error"] + case.atol
                                    + rtol * float(exact.abs().max())))
    return detail


def _near_greedy(token, want_logits, exact_logits, case: Case) -> bool:
    """Accept a greedy token whose FP32 logit is within the logits band of the top.

    The margin is the larger of the BF16 reference's RMS logit error and the
    peak error that the logits band in ``_band`` accepts. Logits that pass the
    band can change order within that margin, for example after a change of
    summation order, so their greedy token must pass too.
    """
    import torch

    exact_logits = exact_logits.double()
    error = want_logits.double() - exact_logits
    rtol = case.bf16_rtol if want_logits.dtype == torch.bfloat16 else case.rtol
    peak = (case.noise_factor * float(error.abs().max()) + case.atol
            + rtol * float(exact_logits.abs().max()))
    margin = max(case.noise_factor * float(error.square().mean().sqrt()), peak)
    return bool(exact_logits[token] >= exact_logits.max() - margin)


# Sequential codebook decoding conditions each code on the earlier ones, so a
# BF16 near-tie early in a frame changes every later code. Grading follows the
# submission's own codes, but only a few near-tie departures are accepted.
MAX_CODEBOOK_DIVERGENCES = 4


def _teacher_forced(case: Case, inputs: dict, expected: dict, exact: dict,
                    actual: dict) -> tuple[dict, dict]:
    """Grade codebook frames against references forced onto the submission's codes.

    The unforced oracle may itself leave the BF16 reference's path at a
    near-tie, so both references always condition on the submitted codes.
    Each code must be the forced BF16 reference's or FP32 oracle's greedy
    choice, or within the BF16 band of the forced oracle's top logit.
    """
    import torch

    from ..tasks.oracle import fp32_reference
    from ..tasks.workloads import _module

    got, want = actual.get("codes"), expected["codes"]
    if (not isinstance(got, torch.Tensor) or got.shape != want.shape or got.dtype != want.dtype
            or got.device != want.device):
        return expected, exact
    module = _module(case)
    forced = got.clone()
    if not torch.equal(got, want):
        expected = module.reference(case, inputs, forced_codes=forced)
    exact = fp32_reference(module, case, inputs, forced_codes=forced)
    departures = []
    for index, code in enumerate(got.tolist()):
        if code in (int(expected["logits"][index].argmax()), int(exact["logits"][index].argmax())):
            continue
        if not _near_greedy(code, expected["logits"][index], exact["logits"][index], case):
            raise AssertionError(f"codes: codebook {index} chose {code}, outside the greedy band")
        departures.append(index)
    if len(departures) > MAX_CODEBOOK_DIVERGENCES:
        raise AssertionError(f"codes: {len(departures)} near-tie departures at {departures}")
    return expected, exact


# TileRT-style per-step gate. Every floating output must point the same way as
# the FP32 oracle and stay within a relative L2 error of it; the greedy token
# must be the reference's or oracle's, or a near-tie of the oracle's top logit.
# Routing and sparse-selection integers are reported, not graded, so any
# implementation of the same math (sparse attention, other summation orders)
# can pass.
# Calibrated on TileRT 0.1.6 over the 12 real-weight prompt windows (Slurm
# 7208-7210). Its worst window passes at cosine 0.990 / relative L2 0.148
# (ctx32k seed 0, against the reference), and the reference's own logits drift
# to 0.985 / 0.175 from the oracle on ctx4k seed 0. The two limits match
# (relative L2 = sqrt(2 * (1 - cosine)) at equal norms) and leave about 2x
# headroom in 1 - cosine over TileRT's worst window.
TILERT_MIN_COSINE = 0.98
TILERT_MAX_RELATIVE_L2 = 0.20
# Cache writes are graded over the first layers only (DeepSeek-V3.2: three
# dense layers and the first MoE layer); later layers are reported. Near-tie
# sparse-index and expert flips compound with depth: on one 4K prompt the
# BF16 reference's cache writes reach cosine 0.87 to the oracle over all 61
# layers, but stay within 2.3% relative L2 over these four.
TILERT_GRADED_LAYERS = 4
TILERT_CACHE_WRITES = ("kv_write", "pe_write", "index_k_write")
TILERT_DIAGNOSTIC = ("sparse_indices", "expert_ids")


def _tp_sharded(case: Case) -> bool:
    import torch.distributed as dist

    return dist.is_available() and dist.is_initialized() and case.tp > 1 and dist.get_world_size() == case.tp


def _group_total(sharded: bool):
    """In-place sum (or ``op``) over the TP group; the identity outside one."""
    import torch.distributed as dist

    def total(value, op=None):
        if sharded:
            dist.all_reduce(value, op=op or dist.ReduceOp.SUM)
        return value
    return total


def _check_structure(expected: dict, actual: dict, device: str, sharded: bool) -> None:
    """Raise on every rank if any rank's outputs have the wrong keys, shapes, dtypes or device."""
    import torch
    import torch.distributed as dist

    problem = None
    if not isinstance(actual, dict) or set(actual) != set(expected):
        problem = (f"output keys: expected {sorted(expected)}, got "
                   f"{sorted(actual) if isinstance(actual, dict) else type(actual)}")
    else:
        for name, want in expected.items():
            got = actual[name]
            if not isinstance(got, torch.Tensor):
                problem = f"{name}: output must be a Tensor"
            elif got.shape != want.shape or got.dtype != want.dtype:
                problem = (f"{name}: expected {tuple(want.shape)} {want.dtype}, "
                           f"got {tuple(got.shape)} {got.dtype}")
            elif got.device != want.device:
                problem = f"{name}: output is on {got.device}, expected {device}"
            if problem:
                break
    flag = torch.tensor([problem is not None], device=device, dtype=torch.int32)
    if sharded:
        dist.all_reduce(flag, op=dist.ReduceOp.MAX)
    if problem or flag.item():
        raise AssertionError(problem or "another rank returned malformed outputs")


def _similarity(got, want, oracle, total=None) -> dict:
    """Cosine and relative L2 to the oracle and to the reference; ``total`` reduces shards.

    Passes within ``TILERT_MIN_COSINE``/``TILERT_MAX_RELATIVE_L2`` of either.
    """
    import torch

    got, want, oracle = (value.double().flatten() for value in (got, want, oracle))
    sums = torch.stack([got @ oracle, got @ want, got.square().sum(), oracle.square().sum(),
                        want.square().sum(), (got - oracle).square().sum(), (got - want).square().sum(),
                        want @ oracle, (want - oracle).square().sum()])
    if total is not None:
        sums = total(sums)
    dot, ref_dot, got_sq, oracle_sq, ref_sq, error_sq, ref_error_sq, agree, drift_sq = sums.tolist()
    tiny = torch.finfo(torch.float64).tiny
    result = {"cosine": dot / max((got_sq * oracle_sq) ** 0.5, tiny),
              "relative_l2": (error_sq / max(oracle_sq, tiny)) ** 0.5,
              "cosine_to_reference": ref_dot / max((got_sq * ref_sq) ** 0.5, tiny),
              "relative_l2_to_reference": (ref_error_sq / max(ref_sq, tiny)) ** 0.5,
              "reference_cosine": agree / max((ref_sq * oracle_sq) ** 0.5, tiny),
              "reference_relative_l2": (drift_sq / max(oracle_sq, tiny)) ** 0.5}
    result["pass"] = any(result[cosine] >= TILERT_MIN_COSINE and result[relative] <= TILERT_MAX_RELATIVE_L2
                         for cosine, relative in (("cosine", "relative_l2"),
                                                  ("cosine_to_reference", "relative_l2_to_reference")))
    return result


def _recall(got, want) -> dict:
    """Ungraded overlap of submitted index rows with the reference's (``-1`` is padding)."""
    got, want = got.reshape(got.shape[0], -1), want.reshape(want.shape[0], -1)
    recall = [len(set(row.tolist()) & set(ref.tolist()) - {-1}) / max(len(set(ref.tolist()) - {-1}), 1)
              for row, ref in zip(got.cpu(), want.cpu()) if (ref >= 0).any()]
    return {"graded": False, "mean_recall": sum(recall) / max(len(recall), 1),
            "min_recall": min(recall, default=1.0)}


def tilert_metrics(expected: dict, actual: dict, case: Case, device: str, exact: dict) -> dict:
    """Per-output cosine similarity and relative L2, with verdicts.

    Each floating output is measured against both the FP32 oracle and the
    BF16 reference, and passes if it is within the thresholds of either.
    Cache writes are graded over their first ``TILERT_GRADED_LAYERS`` layers
    and reported over all layers.

    In a TP process group the logits are vocabulary shards, so their sums,
    maxima and the greedy token's logit are reduced over the group. Every rank
    reaches every collective, even when its own outputs are malformed.
    """
    import torch
    import torch.distributed as dist

    sharded = _tp_sharded(case)
    _check_structure(expected, actual, device, sharded)
    total = _group_total(sharded)

    def measure(got: torch.Tensor, want: torch.Tensor, oracle: torch.Tensor, reduce: bool) -> dict:
        return _similarity(got, want, oracle, total if reduce else None)

    details = {}
    for name, want in expected.items():
        if not want.is_floating_point():
            continue
        got, oracle = actual[name], exact[name]
        details[name] = measure(got, want, oracle, name == "logits")
        if name in TILERT_CACHE_WRITES and want.shape[0] > TILERT_GRADED_LAYERS:
            details[name]["graded"] = False
            details[name].pop("pass")
            graded = slice(0, TILERT_GRADED_LAYERS)
            details[f"{name}[:{TILERT_GRADED_LAYERS}]"] = measure(got[graded], want[graded], oracle[graded], False)

    if "next_token" not in expected:
        return details
    # Greedy token: the oracle logit it selects against the oracle maximum.
    token, logits, oracle_logits = int(actual["next_token"]), expected["logits"], exact["logits"].double()
    offset = dist.get_rank() * logits.numel() if sharded else 0
    vocabulary = logits.numel() * (case.tp if sharded else 1)
    picked = torch.zeros((), device=device, dtype=torch.float64)
    if not 0 <= token < vocabulary:
        picked -= torch.inf
    elif offset <= token < offset + logits.numel():
        picked += oracle_logits[token - offset]
    picked = total(picked)
    maximum = total(oracle_logits.max().clone(), dist.ReduceOp.MAX)
    noise = total(torch.stack([(logits.double() - oracle_logits).square().sum(),
                               torch.tensor(float(logits.numel()), device=device, dtype=torch.float64)]))
    margin = case.noise_factor * float((noise[0] / noise[1]).sqrt())
    details["next_token"] = {"token": token, "reference_token": int(expected["next_token"]),
                             "oracle_token": int(exact["next_token"]),
                             "oracle_logit_gap": float(maximum - picked), "near_tie_margin": margin}
    details["next_token"]["pass"] = (token in (details["next_token"]["reference_token"],
                                               details["next_token"]["oracle_token"])
                                     or float(maximum - picked) <= margin)

    for name in (name for name in TILERT_DIAGNOSTIC if name in expected):
        details[name] = _recall(actual[name], expected[name])
    return details


def _tilert_gate(expected: dict, actual: dict, case: Case, device: str, exact: dict) -> dict:
    """Raise unless every graded output passes ``tilert_metrics``."""
    details = tilert_metrics(expected, actual, case, device, exact)
    failed = [name for name, detail in details.items() if detail.get("pass") is False]
    if failed:
        raise AssertionError("; ".join(f"{name}: {details[name]}" for name in failed))
    return details


# DSpark speculative iteration (gate "dspark"). Floating outputs use the
# oracle band of ``_band`` on relative L2 only: error within ``noise_factor``
# times the BF16 reference's own error plus rtol, against the oracle or the
# reference. FP8 re-quantization flips make peak errors meaningless. Stacked
# per-layer writes are graded layer by layer, so one wrong layer cannot hide
# in the average; smaller per-source writes are graded whole. DSpark outputs
# read the hidden state entering the last backbone layers, so their reference
# error is floored at the deepest window write's. Expert and sparse-index
# selections must keep DSPARK_MIN_RECALL of the reference's picks on average
# in every layer, source and stage. Greedy picks are graded per row, because each verify row and
# Markov step can tie within BF16 noise; the task's ``regrade`` hook has
# already teacher-forced the reference and oracle onto the submission's picks,
# so the acceptance outputs must then match exactly. Calibration is in
# docs/NON_P0_VALIDATION.md.
DSPARK_TOKEN_ROWS = (("verify_tokens", "logits"), ("draft_tokens", "draft_logits"))
DSPARK_EXACT = ("accepted_count", "committed_tokens")
DSPARK_VOCAB_SHARDS = ("logits", "draft_logits")
DSPARK_PER_LAYER = ("window_kv_write", "draft_window_kv_write")
# Reference-error floors for DSpark outputs, in units of the deepest window
# write's reference error. The five confidences are one projection each and
# their relative error over five values is noisy, hence the wider floor.
DSPARK_DRAFT = {"draft_logits": 1.0, "draft_confidence": 3.0, "draft_window_kv_write": 1.0}
# Legitimate BF16 schedules kept >= 0.75 (experts per layer), >= 0.89 (sparse
# indices per source) and >= 0.87 (DSpark experts per stage); random picks
# score about 0.02, 0.25-0.5 and 0.02.
DSPARK_MIN_RECALL = {"expert_ids": 0.5, "sparse_indices": 0.7, "draft_expert_ids": 0.5}


def _relative_band(got, want, oracle, case: Case, total=None, floor: float = 0.0) -> dict:
    """``_band``'s relative-L2 rule against the oracle or the reference, shard-reduced by ``total``.

    ``floor`` is a lower bound on the reference error the limit scales with.
    """
    import torch

    got, want, oracle = (value.double().flatten() for value in (got, want, oracle))
    sums = torch.stack([(got - oracle).square().sum(), (got - want).square().sum(),
                        (want - oracle).square().sum(), oracle.square().sum(), got.square().sum()])
    if total is not None:
        sums = total(sums)
    error, to_reference, drift, scale, got_sq = sums.tolist()
    if scale == 0 and drift == 0:
        return {"relative_error": got_sq ** 0.5, "reference_relative_error": 0.0, "pass": got_sq == 0}
    scale = max(scale, torch.finfo(torch.float64).tiny) ** 0.5
    rtol = case.bf16_rtol if want.dtype == torch.bfloat16 else case.rtol
    detail = {"relative_error": error ** 0.5 / scale,
              "relative_error_to_reference": to_reference ** 0.5 / scale,
              "reference_relative_error": drift ** 0.5 / scale}
    limit = case.noise_factor * max(detail["reference_relative_error"], floor) + rtol
    detail["pass"] = bool(min(detail["relative_error"], detail["relative_error_to_reference"]) <= limit)
    return detail


def _row_picks(picks, got_logits, want_logits, oracle_logits, case: Case, device: str, sharded: bool) -> dict:
    """Near-tie rule for each row of vocabulary-sharded logits.

    A pick passes if it is the BF16 reference's or the oracle's greedy token
    for its row, or its oracle logit is within ``noise_factor`` times the
    row's relative logit error (the reference's or the submission's,
    whichever is larger; the logits band bounds the latter) times the row's
    largest oracle logit magnitude. Errors grow with the logit, so the RMS
    error alone understates the error at the top of the row.
    """
    import torch
    import torch.distributed as dist

    total = _group_total(sharded)
    width = want_logits.shape[-1]
    offset = dist.get_rank() * width if sharded else 0
    picks, want, oracle = picks.reshape(-1), want_logits.double(), oracle_logits.double()
    local = picks - offset
    inside = (local >= 0) & (local < width)
    picked = torch.where(inside, oracle.gather(1, local.clamp(0, width - 1)[:, None])[:, 0],
                         torch.zeros_like(picks, dtype=torch.float64))
    picked = total(picked)
    vocabulary = width * (case.tp if sharded else 1)
    picked = torch.where((picks >= 0) & (picks < vocabulary), picked, -torch.inf)

    def argmax(values):
        maximum = total(values.amax(-1), dist.ReduceOp.MAX)
        ids = torch.arange(width, device=values.device) + offset
        best = torch.where(values == maximum[:, None], ids, torch.iinfo(torch.int64).max).amin(-1)
        return maximum, total(best, dist.ReduceOp.MIN)

    oracle_max, oracle_best = argmax(oracle)
    _, want_best = argmax(want)
    noise = total(torch.stack([(want - oracle).square().sum(-1), (got_logits.double() - oracle).square().sum(-1),
                               oracle.square().sum(-1)]))
    peak = total(oracle.abs().amax(-1), dist.ReduceOp.MAX)
    relative = (noise[:2].amax(0) / noise[2].clamp_min(torch.finfo(torch.float64).tiny)).sqrt()
    margin = case.noise_factor * relative * peak
    gap = oracle_max - picked
    passed = (picks == want_best) | (picks == oracle_best) | (gap <= margin)
    return {"tokens": picks.tolist(), "reference_tokens": want_best.tolist(),
            "oracle_tokens": oracle_best.tolist(), "oracle_logit_gap": gap.tolist(),
            "near_tie_margin": margin.tolist(), "pass": bool(passed.all())}


def dspark_metrics(expected: dict, actual: dict, case: Case, device: str, exact: dict) -> dict:
    """Similarity of float outputs, per-row greedy picks and exact acceptance outputs."""
    import torch

    sharded = _tp_sharded(case)
    _check_structure(expected, actual, device, sharded)
    total = _group_total(sharded)
    want_last, oracle_last = (value["window_kv_write"][-1].double() for value in (expected, exact))
    deepest = float((want_last - oracle_last).norm()) / max(float(oracle_last.norm()), torch.finfo(torch.float64).tiny)
    details = {}
    for name, want in expected.items():
        if not want.is_floating_point():
            continue
        got, oracle = actual[name], exact[name]
        floor = deepest * DSPARK_DRAFT.get(name, 0.0)
        if name in DSPARK_PER_LAYER:
            for layer in range(want.shape[0]):
                details[f"{name}[{layer}]"] = _relative_band(got[layer], want[layer], oracle[layer], case,
                                                             floor=floor)
        else:
            details[name] = _relative_band(got, want, oracle, case, total if name in DSPARK_VOCAB_SHARDS else None,
                                           floor=floor)
    for name, logits in DSPARK_TOKEN_ROWS:
        details[name] = _row_picks(actual[name], actual[logits], expected[logits], exact[logits], case, device,
                                   sharded)
    for name in DSPARK_EXACT:
        mismatch = int((actual[name] != expected[name]).sum())
        details[name] = {"mismatched_elements": mismatch, "pass": mismatch == 0}
    for name, minimum in DSPARK_MIN_RECALL.items():
        per_slice = [_recall(actual[name][index], expected[name][index])["mean_recall"]
                     for index in range(expected[name].shape[0])]
        worst = min(range(len(per_slice)), key=per_slice.__getitem__)
        details[name] = {"mean_recall": sum(per_slice) / len(per_slice), "worst_slice": worst,
                         "worst_slice_recall": per_slice[worst], "minimum": minimum,
                         "pass": per_slice[worst] >= minimum}
    return details


def _dspark_gate(expected: dict, actual: dict, case: Case, device: str, exact: dict) -> dict:
    """Raise unless every graded output passes ``dspark_metrics``."""
    details = dspark_metrics(expected, actual, case, device, exact)
    failed = [name for name, detail in details.items() if detail.get("pass") is False]
    if failed:
        raise AssertionError("; ".join(f"{name}: {details[name]}" for name in failed))
    return details


def _compare(expected: dict, actual: dict, case: Case, device: str,
             exact: dict | None = None) -> dict:
    """Check outputs against the reference, or within the BF16 band of ``exact``.

    Without ``exact`` the comparison uses fixed tolerances around ``expected``.
    With the FP32 oracle, each integer element must match the BF16 reference
    or the oracle. A greedy token may also be any token whose FP32 logit is
    within the band of the top logit.
    """
    import torch

    if exact is not None and case.gate == "tilert":
        return _tilert_gate(expected, actual, case, device, exact)
    if exact is not None and case.gate == "dspark":
        return _dspark_gate(expected, actual, case, device, exact)
    if not isinstance(actual, dict) or set(actual) != set(expected):
        raise AssertionError(f"output keys: expected {sorted(expected)}, got "
                             f"{sorted(actual) if isinstance(actual, dict) else type(actual)}")
    details = {}
    for name, want in expected.items():
        got = actual[name]
        if not isinstance(got, torch.Tensor):
            raise AssertionError(f"{name}: output must be a Tensor")
        if got.shape != want.shape or got.dtype != want.dtype:
            raise AssertionError(f"{name}: expected {tuple(want.shape)} {want.dtype}, "
                                 f"got {tuple(got.shape)} {got.dtype}")
        if got.device != want.device:
            raise AssertionError(f"{name}: output is on {got.device}, expected {device}")
        if exact is not None and got.numel() == 0:
            details[name] = {"pass": True}
        elif exact is not None and want.is_floating_point():
            details[name] = _band(want, got, exact[name], case)
            if not details[name]["pass"]:
                raise AssertionError(f"{name}: {details[name]}")
        elif exact is not None:
            oracle_value = exact[name].to(want.dtype)
            neither = (got != want) & (got != oracle_value)
            details[name] = {"mismatched_elements": int((got != want).sum().item()),
                             "oracle_mismatched_elements": int((got != oracle_value).sum().item()),
                             "unmatched_elements": int(neither.sum().item())}
            accepted = details[name]["unmatched_elements"] == 0
            logits = expected.get("logits")
            if (not accepted and name == "next_token" and case.tp == 1
                    and logits is not None and logits.ndim == got.ndim + 1):
                # One greedy token per logits row; check each unmatched row.
                rows = logits.reshape(-1, logits.shape[-1])
                exact_rows = exact["logits"].reshape(rows.shape)
                tokens = got.reshape(-1)
                accepted = all(_near_greedy(int(tokens[row]), rows[row], exact_rows[row], case)
                               for row in neither.reshape(-1).nonzero().flatten().tolist())
            details[name]["pass"] = accepted
            if not accepted:
                raise AssertionError(f"{name}: {details[name]}")
        elif want.is_floating_point():
            delta = (got.float() - want.float()).abs()
            rtol = case.bf16_rtol if want.dtype == torch.bfloat16 else case.rtol
            close = torch.isclose(got.float(), want.float(),
                                  atol=case.atol, rtol=rtol, equal_nan=False)
            details[name] = {"max_abs_error": float(delta.max().item()),
                             "mismatched_elements": int((~close).sum().item())}
            if not bool(close.all()):
                raise AssertionError(f"{name}: {details[name]}")
        else:
            mismatch = int((got != want).sum().item())
            details[name] = {"mismatched_elements": mismatch}
            if mismatch:
                raise AssertionError(f"{name}: {mismatch} integer mismatches")
    return details


def check_trials(case: Case, candidate: Callable, device: str,
                 trials: int) -> tuple[dict, float | None]:
    """Check fresh inputs, input preservation, and special acceptance scenarios."""
    import torch

    result = {"status": "pass", "trials": []}
    first_call_ms: float | None = None
    trial_seeds = [secrets.randbits(32) for _ in range(trials)]
    for trial_index, seed in enumerate(trial_seeds):
        inputs = make_inputs(case, seed, device)
        scenario = "seeded"
        if case.family in ("spec_target_step", "spec_full_iteration"):
            scenario = ("eagle3_raw", "accept_full", "accept_one")[trial_index % 3]
            if scenario != "eagle3_raw":
                if case.family == "spec_target_step":
                    from ..tasks.eagle3 import set_acceptance_scenario
                else:
                    from ..tasks.speculative import set_acceptance_scenario
                accepted_prefix = (case.params["draft_depth"] if
                                   scenario == "accept_full" else 1)
                set_acceptance_scenario(case, inputs, accepted_prefix)
        else:
            scenario = set_trial_scenario(case, inputs, trial_index)
        # A keyed fingerprint where each input lives, not a copy: full MoE weights
        # exceed 60 GB, and a host copy of them needs that much host memory.
        originals = input_fingerprints(inputs)
        expected = reference(case, inputs)
        exact = oracle(case, inputs)
        first_start = time.perf_counter() if trial_index == 0 else None
        if trial_index == 0:
            beat("first candidate call (may compile)", allow=900, force=True)
        actual = candidate(inputs)
        if torch.device(device).type == "cuda":
            torch.cuda.synchronize(device)
        beat(f"correctness trial {trial_index}", force=True)
        if first_start is not None:
            first_call_ms = (time.perf_counter() - first_start) * 1000
        for name in mutated_inputs(inputs, originals):
            raise AssertionError(f"candidate mutated input {name}")
        expected, exact = regrade(case, inputs, expected, exact, actual)
        if case.family == "tts_frame_step":
            forced = _teacher_forced(case, inputs, expected, exact, actual)
            if forced[0] is not expected:
                scenario = "teacher_forced"
            expected, exact = forced
        details = _compare(expected, actual, case, device, exact)
        trial_record = {"seed": seed, "scenario": scenario, "outputs": details}
        if case.family in ("spec_target_step", "spec_full_iteration"):
            trial_record["spec_accounting"] = {
                "proposed_draft_tokens": case.params["draft_depth"],
                "accepted_draft_tokens": int(expected["accepted_count"].item()),
                "committed_tokens": int(expected["committed_count"].item()),
            }
        result["trials"].append(trial_record)
        del inputs, originals, expected, exact, actual
    return result, first_call_ms
