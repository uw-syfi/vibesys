"""Accuracy gate: compare a candidate engine against HF transformers golden outputs.
Run from the bundle root:
    # any running OpenAI-compatible server (the default, and what vibesys runs)
    uv run python accuracy_checker/checker.py --base-url http://127.0.0.1:8000
    # in-process reference engine
    uv run python accuracy_checker/checker.py --target inproc
Exit status 0 = PASS, 1 = FAIL. Policy and thresholds: see README.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Runnable as a script (`python accuracy_checker/checker.py`) or as a module:
# both `accuracy_checker` and `reference` are packages under the bundle root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from accuracy_checker.resume import evaluate_resume
from accuracy_checker.targets import EngineTarget, HFTarget, HttpTarget, Target
from accuracy_checker.thresholds import Thresholds

GOLDEN = Path(__file__).with_name("golden.json")


@dataclass
class CaseResult:
    name: str
    prompt_tokens: int
    gen_tokens: int
    prefix_match: int  # leading greedy tokens equal to golden
    divergence_margin: float | None  # golden margin at the first divergence, None if identical
    decisive_flips: int
    mean_abs_dlogprob: float
    max_abs_dlogprob: float


def _p99(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.99 * len(xs)))]


def evaluate(target: Target, golden: dict, th: Thresholds, log=print) -> tuple[bool, dict]:
    results: list[CaseResult] = []
    all_dlp: list[float] = []
    for case in golden["cases"]:
        prompt, gold = case["prompt_ids"], case["greedy_ids"]
        margins = case["tf_margin"]
        got = target.greedy(prompt, len(gold))
        prefix = next(
            (i for i, (a, b) in enumerate(zip(got, gold, strict=False)) if a != b),
            min(len(got), len(gold)),
        )
        if len(got) != len(gold) and prefix == min(len(got), len(gold)):
            raise RuntimeError(
                f"{case['name']}: target returned {len(got)} tokens, expected {len(gold)}"
            )
        div_margin = margins[prefix] if prefix < len(gold) else None
        forced = target.teacher_forced(prompt, gold)
        dlp = [abs(f.logprob - g) for f, g in zip(forced, case["tf_logprob"], strict=True)]
        flips = sum(
            1
            for f, top1, m in zip(forced, case["tf_top1"], margins, strict=True)
            if m >= th.near_tie_margin and f.argmax != top1
        )
        all_dlp += dlp
        r = CaseResult(
            name=case["name"],
            prompt_tokens=len(prompt),
            gen_tokens=len(gold),
            prefix_match=prefix,
            divergence_margin=div_margin,
            decisive_flips=flips,
            mean_abs_dlogprob=statistics.fmean(dlp),
            max_abs_dlogprob=max(dlp),
        )
        results.append(r)
        div = (
            "identical"
            if div_margin is None
            else f"diverge@{prefix} (golden margin {div_margin:.3f})"
        )
        log(
            f"  {r.name:<15} prompt={r.prompt_tokens:>5} {div:<34} flips={flips} "
            f"mean|dlp|={r.mean_abs_dlogprob:.4f} max|dlp|={r.max_abs_dlogprob:.4f}"
        )
    non_tie_divergences = [
        r.name
        for r in results
        if r.divergence_margin is not None and r.divergence_margin >= th.near_tie_margin
    ]
    summary = {
        "target": target.name,
        "thresholds": asdict(th),
        "identical_cases": sum(r.divergence_margin is None for r in results),
        "cases": len(results),
        "non_tie_divergences": non_tie_divergences,
        "decisive_flips": sum(r.decisive_flips for r in results),
        "mean_abs_dlogprob": statistics.fmean(all_dlp),
        "p99_abs_dlogprob": _p99(all_dlp),
        "max_abs_dlogprob": max(all_dlp),
        "mean_prefix_fraction": statistics.fmean(r.prefix_match / r.gen_tokens for r in results),
        "per_case": [asdict(r) for r in results],
    }
    checks = {
        "free-run divergences only at near-ties": not non_tie_divergences,
        f"decisive teacher-forced flips <= {th.max_decisive_flips}": summary["decisive_flips"]
        <= th.max_decisive_flips,
        f"mean |dlogprob| <= {th.max_mean_abs_dlogprob}": summary["mean_abs_dlogprob"]
        <= th.max_mean_abs_dlogprob,
        f"p99 |dlogprob| <= {th.max_p99_abs_dlogprob}": summary["p99_abs_dlogprob"]
        <= th.max_p99_abs_dlogprob,
        f"mean prefix fraction >= {th.min_mean_prefix_fraction}": summary["mean_prefix_fraction"]
        >= th.min_mean_prefix_fraction,
    }
    summary["checks"] = checks
    passed = all(checks.values())
    summary["passed"] = passed
    log(
        f"identical {summary['identical_cases']}/{summary['cases']}, non-tie divergences {non_tie_divergences}, "
        f"decisive flips {summary['decisive_flips']}, mean|dlp| {summary['mean_abs_dlogprob']:.4f}, "
        f"p99|dlp| {summary['p99_abs_dlogprob']:.4f}, max|dlp| {summary['max_abs_dlogprob']:.4f}, "
        f"mean prefix fraction {summary['mean_prefix_fraction']:.3f}"
    )
    for name, ok in checks.items():
        log(f"  [{'ok' if ok else 'FAIL'}] {name}")
    log("PASS" if passed else "FAIL")
    return passed, summary


def run_gate(target: Target, golden: dict, th: Thresholds, log=print) -> tuple[bool, dict]:
    """The full gate: base checks, plus the resume and stream checks for a server target."""
    if not isinstance(target, HttpTarget):
        return evaluate(target, golden, th, log)
    # Resume check first, so its chained rounds resume from the end of a finished
    # request, not from the middle of the base check's 64-token generation.
    log("cache-resume check:")
    resume_passed, resume = evaluate_resume(target, golden, th, log)
    log("base checks:")
    passed, summary = evaluate(target, golden, th, log)
    summary["resume_check"] = resume
    case = golden["cases"][0]
    err = target.stream_matches(case["prompt_ids"], 16, target.greedy(case["prompt_ids"], 16))
    summary["stream_check"] = err or "ok"
    log(f"stream protocol check: {err or 'ok'}")
    passed = passed and resume_passed and err is None
    summary["passed"] = passed
    log(f"gate: {'PASS' if passed else 'FAIL'}")
    return passed, summary


# ----------------------------------------------------------------------------- fault injection
def inject_fault(engine, spec: str) -> None:
    """Deliberately break the in-process model to prove the gate has teeth.
    gdn-decay-off:L  -> layer L's GDN decay gate is disabled (A_log = -inf, so g = 0: the state never decays).
    attn-gate-off:L  -> layer L's attention output gate is disabled (gate logits zeroed, sigmoid = 0.5).
    """
    import torch

    kind, _, idx = spec.partition(":")
    layer = engine.model.layers[int(idx)]
    with torch.no_grad():
        match kind:
            case "gdn-decay-off":
                layer.linear_attn.A_log.fill_(float("-inf"))
            case "attn-gate-off":
                attn = layer.self_attn
                w = attn.q_proj.weight.view(attn.num_heads, 2 * attn.head_dim, -1)
                w[:, attn.head_dim :].zero_()
            case _:
                raise ValueError(f"unknown fault {spec!r}")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--target", choices=["inproc", "http", "hf", "hf-nofla"], default="http")
    p.add_argument("--model", default="Qwen/Qwen3.5-9B")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--golden", type=Path, default=GOLDEN)
    p.add_argument("--fault", action="append", default=[], help="inproc only; see inject_fault")
    p.add_argument("--json-out", type=Path, default=None)
    args = p.parse_args()
    golden = json.loads(args.golden.read_text())
    print(
        f"golden: {golden['meta']['source']} on {golden['meta']['device']}, {len(golden['cases'])} cases"
    )
    target: Target
    match args.target:
        case "inproc":
            from reference.engine import Engine

            engine = Engine(args.model)
            for spec in args.fault:
                inject_fault(engine, spec)
            target = EngineTarget(engine)
            if args.fault:
                target.name += "+" + ",".join(args.fault)
        case "http":
            target = HttpTarget(args.base_url, args.model)
        case "hf" | "hf-nofla":
            target = HFTarget(args.model, use_fla=args.target == "hf")
    print(f"target: {target.name}")
    passed, summary = run_gate(target, golden, Thresholds())
    if args.json_out:
        args.json_out.write_text(json.dumps(summary, indent=1))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
