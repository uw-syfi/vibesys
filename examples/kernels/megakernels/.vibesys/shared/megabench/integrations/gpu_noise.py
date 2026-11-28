"""Measure timing noise on the GPU and say when one timing cannot be trusted.

A process can land in a slow mode: every launch in that process (or of one loaded
candidate) is slower by a large, constant factor, while a fresh process on the same
GPU is not. One timing then looks like a regression or hides a gain. This module
gives three things:

* ``probe``: time a submission in a fresh process (build, first call, steady p50) and
  time a fixed canary (one bf16 GEMM and one device copy) before and after it. A slow
  canary in the same process points at the GPU or node; a normal canary points at the
  loaded candidate.
* ``check``: run ``megabench evaluate`` and then ``probe`` in more fresh processes,
  compare the processes and print a stability verdict and the p50 of the fast cluster.
* ``NodeSampler``: on the host, sample clocks, power, temperature, throttle reasons and
  utilization of every GPU of the node; the GPU broker reports them for each job.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

DEFAULT_TOLERANCE_PCT = 3.0

QUERY = ("index,uuid,clocks.sm,clocks.mem,power.draw,temperature.gpu,"
         "clocks_event_reasons.active,utilization.gpu")

# nvmlClocksEventReasons bits; GpuIdle (0x1) is normal between launches and is not reported.
REASONS = {0x2: "applications clocks setting", 0x4: "software power cap",
           0x8: "hardware slowdown", 0x10: "sync boost", 0x20: "software thermal slowdown",
           0x40: "hardware thermal slowdown", 0x80: "hardware power brake",
           0x100: "display clock setting"}


def decode_reasons(mask: int) -> list[str]:
    return [name for bit, name in REASONS.items() if mask & bit]


# ---------------------------------------------------------------- stability

def spread(values: list[float], tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> dict:
    """Compare per-process timings of one candidate.

    The slow mode only makes a process slower, so the fast cluster (the values within
    the tolerance of the fastest) gives the candidate's time. ``slow`` lists the
    indexes of the processes outside it.
    """
    if not values:
        return {"verdict": "no data"}
    low = min(values)
    limit = low * (1 + tolerance_pct / 100)
    fast = [v for v in values if v <= limit]
    slow = [i for i, v in enumerate(values) if v > limit]
    return {"values_ms": [round(v, 4) for v in values],
            "median_ms": statistics.median(values),
            "fast_p50_ms": statistics.median(fast),
            "spread_pct": round(100 * (max(values) - low) / low, 2),
            "tolerance_pct": tolerance_pct,
            "slow_processes": slow,
            "verdict": "stable" if not slow else "unstable"}


def canary_note(canaries: list[dict | None], slow: list[int],
                tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> str:
    """Say whether the slow processes were also slow on the canary."""
    times = [c.get("gemm_us", 0) + c.get("copy_us", 0) if c else None for c in canaries]
    known = [t for t in times if t]
    if not slow or not known:
        return ""
    base = min(known) * (1 + tolerance_pct / 100)
    hit = [i for i in slow if times[i] and times[i] > base]
    if hit:
        return ("the canary was also slow in process(es) " + ", ".join(map(str, hit))
                + ": the GPU or node was slow, not the candidate")
    return ("the canary was normal in the slow process(es): the slowdown belongs to "
            "the loaded candidate instance, not to the GPU")


def format_stability(label: str, stab: dict, extra: list[dict] | None = None,
                     note: str = "") -> str:
    if stab.get("verdict") == "no data":
        return f"timing stability ({label}): no data"
    values = ", ".join(f"{v:.3f}" for v in stab["values_ms"])
    lines = [f"timing stability ({label}): {stab['verdict'].upper()} over "
             f"{len(stab['values_ms'])} fresh processes: p50 {values} ms "
             f"(spread {stab['spread_pct']}%, tolerance {stab['tolerance_pct']}%)",
             f"  fast-cluster p50: {stab['fast_p50_ms']:.4f} ms"]
    if stab["slow_processes"]:
        lines.append("  slow process(es): " + ", ".join(map(str, stab["slow_processes"]))
                     + "; their time is GPU noise, not a property of the candidate")
    if note:
        lines.append("  " + note)
    for i, row in enumerate(extra or []):
        bits = [f"build {row['build_ms']:.0f} ms" if row.get("build_ms") is not None else "",
                f"first call {row['first_call_ms']:.1f} ms"
                if row.get("first_call_ms") is not None else "",
                (f"canary gemm {row['canary']['gemm_us']:.0f} us copy "
                 f"{row['canary']['copy_us']:.0f} us") if row.get("canary") else "",
                f"gpu {row['gpu_uuid'][:12]}" if row.get("gpu_uuid") else ""]
        lines.append(f"  process {i}: " + ", ".join(b for b in bits if b))
    return "\n".join(lines)


# ---------------------------------------------------------------- in-process probes

def canary(device: str = "cuda:0", reps: int = 20) -> dict:
    """Median time of a fixed bf16 GEMM and a fixed device copy, in microseconds."""
    import torch

    a = torch.randn(4096, 4096, device=device, dtype=torch.bfloat16)
    b = torch.randn(4096, 4096, device=device, dtype=torch.bfloat16)
    src = torch.empty(256 * 1024 * 1024, device=device, dtype=torch.uint8)
    dst = torch.empty_like(src)

    def timed(fn) -> float:
        for _ in range(3):
            fn()
        out = []
        for _ in range(reps):
            start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
            start.record()
            fn()
            end.record()
            end.synchronize()
            out.append(start.elapsed_time(end) * 1000)
        return statistics.median(out)

    result = {"gemm_us": timed(lambda: a @ b), "copy_us": timed(lambda: dst.copy_(src))}
    del a, b, src, dst
    return result


def gpu_uuid(device: str = "cuda:0") -> str | None:
    import torch

    try:
        return "GPU-" + str(torch.cuda.get_device_properties(device).uuid)
    except Exception:  # noqa: BLE001  # older torch has no uuid
        return None


def time_candidate(candidate, inputs, device: str, warmup: int, reps: int) -> list[float]:
    import torch

    with torch.inference_mode():
        for _ in range(warmup):
            candidate(inputs)
        torch.cuda.synchronize(device)
        out = []
        for _ in range(reps):
            start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
            start.record()
            candidate(inputs)
            end.record()
            end.synchronize()
            out.append(start.elapsed_time(end))
    return out


def probe(case, submission: Path, *, device: str = "cuda:0", warmup: int = 3,
          reps: int = 20, seed: int = 7301) -> dict:
    """Fresh-process timing of a submission (weighted over regimes, as evaluate does)."""
    import torch
    from ..harness.runner import _load_submission
    from ..workloads import make_inputs

    shapes = ([(r["weight"], sub) for r, sub in case.regime_cases()] if case.regimes
              else [(1.0, case)])
    total = sum(w for w, _ in shapes)
    row: dict = {"gpu_uuid": gpu_uuid(device), "canary_before": canary(device)}
    p50, build_ms, first_ms = 0.0, 0.0, 0.0
    for weight, shape in shapes:
        start = time.perf_counter()
        candidate = _load_submission(submission, shape)
        build_ms += (time.perf_counter() - start) * 1000
        with torch.inference_mode():
            inputs = make_inputs(shape, seed, device)
            torch.cuda.synchronize(device)
            start = time.perf_counter()
            candidate(inputs)
            torch.cuda.synchronize(device)
            first_ms += (time.perf_counter() - start) * 1000
        p50 += weight / total * statistics.median(
            time_candidate(candidate, inputs, device, warmup, reps))
    row["canary_after"] = canary(device)
    row["canary"] = {k: min(row["canary_before"][k], row["canary_after"][k])
                     for k in ("gemm_us", "copy_us")}
    row.update(p50_ms=p50, build_ms=build_ms, first_call_ms=first_ms)
    return row


def probe_command(case_id: str, submission: Path, regimes: Path | None,
                  python: str = sys.executable) -> list[str]:
    cmd = [python, "-m", "megabench.integrations.gpu_noise", "probe", "--case", case_id,
           "--submission", str(submission)]
    return cmd + (["--regimes", str(regimes)] if regimes else [])


def run_probes(command: list[str], count: int, timeout: float = 600) -> list[dict]:
    """Run *command* (a ``probe``) *count* times, each in a fresh process."""
    rows = []
    for _ in range(count):
        try:
            out = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            line = [x for x in out.stdout.splitlines() if x.startswith("{")]
            rows.append(json.loads(line[-1]) if out.returncode == 0 and line
                        else {"error": (out.stderr or out.stdout)[-500:]})
        except subprocess.TimeoutExpired:
            rows.append({"error": "probe timed out"})
    return rows


def _load_case(case_id: str, regimes: Path | None):
    from ..cases import load_regimes, select_cases

    case = select_cases("all", [case_id])[0]
    return replace(case, regimes=load_regimes(regimes)) if regimes else case


# ---------------------------------------------------------------- stability reruns

MAX_RERUNS = 2  # stability reruns after one UNSTABLE or SLOW result
RERUNNABLE = ("unstable", "slow")

# ---------------------------------------------------------------- timing history
#
# The slow mode only makes a run slower. So the best estimate of one code tree under one
# measurement is the lowest fast-cluster p50 of all its runs, of any verdict: its score. A run
# whose processes agree but which is slower than the tree's best earlier run is in the slow
# mode as a whole (verdict SLOW); its score is the earlier best.

HISTORY_FILE = "timing-history.jsonl"
OFFICIAL_HISTORY_ENV = "MEGABENCH_OFFICIAL_TIMING_HISTORY"  # framework gates only


def history_key(kind: str, case: str, setup: str, identity: str) -> str:
    """One measurement of one code tree: tool, case, regimes or regime, tree digest."""
    return "|".join((kind, case, setup or "", identity))


def best_known(path: Path | None, key: str) -> tuple[float, int] | None:
    """(lowest fast-cluster p50, number of runs) of *key* in the history file at *path*."""
    if path is None or not path.is_file():
        return None
    values = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("key") == key and isinstance(row.get("fast_p50_ms"), (int, float)):
            values.append(float(row["fast_p50_ms"]))
    return (min(values), len(values)) if values else None


def add_history(path: Path | None, key: str, stab: dict, source: str) -> None:
    if path is None or stab.get("fast_p50_ms") is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps({"key": key, "fast_p50_ms": stab["fast_p50_ms"],
                              "verdict": stab.get("verdict"), "source": source,
                              "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")


def score(stab: dict, best: tuple[float, int] | None,
          tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> dict:
    """Score of this run against the tree's earlier runs; updates the verdict in *stab*.

    Adds score_ms (the lower of this fast-cluster p50 and the earlier best), best_known_ms,
    earlier_runs, and sets the verdict to "slow" when all processes agree but the run is
    slower than the earlier best by more than the tolerance.
    """
    fast = stab["fast_p50_ms"]
    if best is None:
        stab.update(score_ms=fast, best_known_ms=None, earlier_runs=0)
        return stab
    low, runs = best
    stab.update(score_ms=min(fast, low), best_known_ms=low, earlier_runs=runs)
    if fast > low * (1 + tolerance_pct / 100) and stab.get("verdict") == "stable":
        stab["verdict"] = "slow"
    return stab


def score_line(stab: dict) -> str:
    if stab.get("score_ms") is None:
        return ""
    if not stab.get("earlier_runs"):
        return (f"  score: {stab['score_ms']:.4f} ms (first run of this code; later runs of the "
                "same code can only lower it)")
    line = (f"  score: {stab['score_ms']:.4f} ms (lowest fast-cluster p50 of this code over "
            f"{stab['earlier_runs'] + 1} runs; earlier best {stab['best_known_ms']:.4f} ms)")
    if stab.get("verdict") == "slow":
        line += ("\n  SLOW: all processes agree, but they are slower than an earlier run of the "
                 "same code; this run was in the slow mode, and the score keeps the earlier best")
    return line


SOURCE_SUFFIXES = {".py", ".cu", ".cuh", ".h", ".hpp", ".cpp", ".cc", ".c", ".json", ".toml"}
NOT_CANDIDATE = {".build", "__pycache__", "reports", ".git", "progress", "roadmap", ".vibesys",
                 ".claude", "megakernel_skills", "primbench", "reference", "state"}


def tree_digest(directory: Path) -> str:
    """Digest of the candidate's source files under *directory* (notes and records excluded)."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        rel = path.relative_to(directory)
        if (path.is_file() and path.suffix in SOURCE_SUFFIXES
                and not NOT_CANDIDATE & set(rel.parts)):
            digest.update(str(rel).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _stability_of(prior: Path) -> tuple[dict | None, str | None]:
    """The stability record and code identity of a check report or a phase_ab result."""
    if prior.suffix == ".jsonl":
        side = prior.with_suffix(".stability.json")
        if not side.is_file():
            return None, None
        stab = json.loads(side.read_text())
        return stab, stab.get("identity")
    data = json.loads(prior.read_text())
    stab = data.get("stability")
    if stab is not None:
        stab = {**stab, "rerun_of": data.get("rerun_of")}
    return stab, data.get("identity")


def rerun_status(prior: Path, identity: str) -> tuple[bool, str]:
    """May this run count as a stability rerun of *prior*? (ok, reason)"""
    if not prior.is_file():
        return False, f"{prior} does not exist"
    stab, prior_identity = _stability_of(prior)
    if stab is None:
        return False, f"{prior} has no timing stability record"
    if stab.get("verdict") not in RERUNNABLE:
        return False, f"{prior} was {stab.get('verdict')}, not unstable or slow"
    if prior_identity != identity:
        return False, f"{prior} timed other code (the candidate files changed since)"
    depth, link = 1, stab.get("rerun_of")
    while link:
        depth += 1
        stab, _ = _stability_of(Path(link)) if Path(link).is_file() else (None, None)
        link = stab.get("rerun_of") if stab else None
    if depth > MAX_RERUNS:
        return False, (f"{prior} is already rerun {depth - 1} time(s); at most {MAX_RERUNS} "
                       "stability reruns follow one unstable result")
    return True, f"stability rerun {depth} of {MAX_RERUNS} for {prior}"


RERUN_NOTE = ("stability rerun: this run repeats an UNSTABLE or SLOW result on the same code; "
              "it is not paid work and does not count against a paid-call cap")


# ---------------------------------------------------------------- check wrapper

def check_report(record: dict, probes: list[dict], tolerance_pct: float) -> dict:
    """Stability of an evaluate record (process 0) and its probe processes."""
    first = {"p50_ms": record["candidate_timing"]["cuda_event_p50_ms"],
             "build_ms": record.get("build_ms"),
             "first_call_ms": record.get("first_call_ms_including_jit"),
             "gpu_uuid": None, "canary": None}
    rows = [first] + [p for p in probes if "p50_ms" in p]
    stab = spread([r["p50_ms"] for r in rows], tolerance_pct)
    stab["note"] = canary_note([r.get("canary") for r in rows], stab["slow_processes"],
                               tolerance_pct)
    stab["processes"] = rows
    stab["probe_errors"] = [p["error"] for p in probes if "error" in p]
    return stab


def check(args: argparse.Namespace) -> int:
    identity = tree_digest(args.submission.resolve().parent)
    if args.rerun_of is not None:
        ok, reason = rerun_status(args.rerun_of, identity)
        if not ok:
            print(f"check_candidate: --rerun-of refused: {reason}", file=sys.stderr)
            return 2
        print(f"{RERUN_NOTE} ({reason})")
    code = subprocess.run(args.evaluate).returncode
    try:
        lines = Path(args.report).read_text().splitlines()
        record = json.loads(lines[-1])
    except (OSError, ValueError, IndexError):
        return code
    if code != 0 or "candidate_timing" not in record or args.processes < 2:
        return code
    probes = run_probes(probe_command(args.case, args.submission, args.regimes),
                        args.processes - 1)
    stab = check_report(record, probes, args.tolerance_pct)
    stab["identity"] = identity
    stab["rerun_of"] = str(args.rerun_of) if args.rerun_of else None
    history = Path(args.report).parent / HISTORY_FILE
    key = history_key("check", args.case, args.regimes.name if args.regimes else "", identity)
    score(stab, best_known(history, key), args.tolerance_pct)
    add_history(history, key, stab, str(args.report))
    Path(args.report).with_suffix(".stability.json").write_text(json.dumps(stab, indent=2))
    print(format_stability("check", stab, stab["processes"], stab["note"]))
    if score_line(stab):
        print(score_line(stab))
    if stab["verdict"] in RERUNNABLE:
        print(f"  to repeat it as a stability rerun: ./check_candidate --rerun-of {args.report}")
    if args.rerun_of:
        print(f"  {RERUN_NOTE}")
    for err in stab["probe_errors"]:
        print(f"  probe failed: {err.strip().splitlines()[-1] if err.strip() else err}")
    print(f"stability: {Path(args.report).with_suffix('.stability.json')}")
    return code


# ---------------------------------------------------------------- host-side sampler

class NodeSampler:
    """Samples every GPU of the node with one long-running ``nvidia-smi`` (host side)."""

    def __init__(self, interval_ms: int = 200, keep: int = 400_000,
                 command: list[str] | None = None) -> None:
        self.samples: deque = deque(maxlen=keep)
        self.lock = threading.Lock()
        self.command = command or ["nvidia-smi", f"--query-gpu={QUERY}",
                                   "--format=csv,noheader,nounits", "-lms", str(interval_ms)]
        self.proc = None

    def start(self) -> "NodeSampler":
        try:
            self.proc = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return self
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def _read(self) -> None:
        for line in self.proc.stdout:
            row = parse_line(line)
            if row is not None:
                with self.lock:
                    self.samples.append((time.time(), row))

    def window(self, t0: float, t1: float) -> list[dict]:
        with self.lock:
            return [row for t, row in self.samples if t0 <= t <= t1]

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()


def _num(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def parse_line(line: str) -> dict | None:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != 8 or not parts[0].isdigit():
        return None
    try:
        mask = int(parts[6], 16)
    except ValueError:
        mask = 0
    return {"index": int(parts[0]), "uuid": parts[1], "sm_mhz": _num(parts[2]),
            "mem_mhz": _num(parts[3]), "power_w": _num(parts[4]), "temp_c": _num(parts[5]),
            "reasons": mask, "util": _num(parts[7])}


def summarize_window(rows: list[dict], gpu_index: int | None) -> dict:
    """The job's GPU while it was busy, and the rest of the node."""
    out: dict = {"samples": len(rows)}
    mine = [r for r in rows if r["index"] == gpu_index]
    if mine:
        busy = [r for r in mine if (r["util"] or 0) > 0] or mine
        sm = [r["sm_mhz"] for r in busy if r["sm_mhz"] is not None]
        mem = [r["mem_mhz"] for r in busy if r["mem_mhz"] is not None]
        mask = 0
        for r in busy:
            mask |= r["reasons"]
        out["gpu"] = {"index": gpu_index, "uuid": mine[0]["uuid"],
                      "sm_mhz_p50": statistics.median(sm) if sm else None,
                      "sm_mhz_min": min(sm) if sm else None,
                      "mem_mhz_min": min(mem) if mem else None,
                      "power_w_max": max((r["power_w"] or 0) for r in mine),
                      "temp_c_max": max((r["temp_c"] or 0) for r in mine),
                      "throttle": decode_reasons(mask)}
    others = [r for r in rows if r["index"] != gpu_index]
    if others:
        by_gpu: dict[int, list[dict]] = {}
        for r in others:
            by_gpu.setdefault(r["index"], []).append(r)
        utils = {i: statistics.mean((r["util"] or 0) for r in rs) for i, rs in by_gpu.items()}
        # Node power per nvidia-smi tick: one tick lists the GPUs in index order.
        sums, tick, last = [], 0.0, None
        for r in rows:
            if last is not None and r["index"] <= last:
                sums.append(tick)
                tick = 0.0
            tick += r["power_w"] or 0
            last = r["index"]
        sums.append(tick)
        out["node"] = {"other_gpus": len(by_gpu),
                       "other_gpus_busy": sum(1 for u in utils.values() if u >= 10),
                       "other_util_mean": round(statistics.mean(utils.values()), 1),
                       "node_power_w_max": round(max(sums), 0)}
    return out


def format_window(name: str, summary: dict) -> str:
    gpu, node = summary.get("gpu"), summary.get("node")
    if not gpu and not node:
        return f"gpu_run: {name}: no GPU samples"
    def f(value) -> str:
        return "?" if value is None else f"{value:.0f}"

    parts = []
    if gpu:
        throttle = ", ".join(gpu["throttle"]) or "none"
        parts.append(f"GPU {gpu['index']} {gpu['uuid'][:12]}: SM clock p50 "
                     f"{f(gpu['sm_mhz_p50'])} MHz (min {f(gpu['sm_mhz_min'])}), memory "
                     f"clock min {f(gpu['mem_mhz_min'])} MHz, power max "
                     f"{f(gpu['power_w_max'])} W, temp max {f(gpu['temp_c_max'])} C, "
                     f"throttle: {throttle}")
    if node:
        parts.append(f"other GPUs on the node: {node['other_gpus_busy']} of "
                     f"{node['other_gpus']} busy (mean util {node['other_util_mean']}%), "
                     f"node GPU power max {f(node['node_power_w_max'])} W")
    return f"gpu_run: {name}: " + "; ".join(parts)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("probe", help="time a submission in this (fresh) process; print JSON")
    p.add_argument("--case", required=True)
    p.add_argument("--submission", type=Path, required=True)
    p.add_argument("--regimes", type=Path)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--reps", type=int, default=20)
    c = sub.add_parser("check", help="megabench evaluate, then probes in fresh processes")
    c.add_argument("--case", required=True)
    c.add_argument("--submission", type=Path, required=True)
    c.add_argument("--regimes", type=Path)
    c.add_argument("--report", required=True, help="the evaluate --output file")
    c.add_argument("--processes", type=int, default=3,
                   help="fresh processes in total, the evaluate process included")
    c.add_argument("--tolerance-pct", type=float, default=DEFAULT_TOLERANCE_PCT)
    c.add_argument("--rerun-of", type=Path,
                   help="an UNSTABLE check report of the same code; marks this run a rerun")
    c.add_argument("evaluate", nargs=argparse.REMAINDER, help="-- the evaluate command")
    args = parser.parse_args(argv)
    if args.mode == "probe":
        case = _load_case(args.case, args.regimes)
        print(json.dumps(probe(case, args.submission.resolve(), warmup=args.warmup,
                               reps=args.reps)))
        return 0
    args.evaluate = args.evaluate[1:] if args.evaluate[:1] == ["--"] else args.evaluate
    if not args.evaluate:
        parser.error("give the evaluate command after --")
    return check(args)


if __name__ == "__main__":
    raise SystemExit(main())
