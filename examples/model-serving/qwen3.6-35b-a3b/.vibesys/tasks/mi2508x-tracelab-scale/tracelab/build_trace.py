#!/usr/bin/env python3
"""Build a saturated-replay benchmark trace from the TraceLab coding-agent dataset.

Pipeline: verify sha256 -> parse JSONL (data only, never executed) -> per-session
rounds sorted by round_index -> drop sessions whose peak context exceeds the cap
-> sample sessions with replacement, give each copy its own mid-session start
point -> write session-rounds-v2 CSV -> run `tracegen coding-session` ->
write build_manifest.json.

Field mapping reproduces TraceLab's convert.py (session-rounds-v2):
  input_len  = max(newly_append_tokens, 1)
  prefix_len = max(prefix_tokens, 0)
  output_len = max(output_tokens, 1)
  tool_wait_after_ms = sum of non-null, non-negative tool_wall_latency_ms of the
                       round's tools; 0 on the session's final round.
Rounds are ordered by (round_index, file line), as convert.py does.

Standard library only; deterministic for fixed arguments.
"""

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import random
import subprocess
import sys

EXPECTED_SHA256 = "11ce51ec0a25e3d1d95b025bca2f7d1647e47571eb7cc968acd5fc64d4b4fb65"
RAW_FIELDS = [
    "session_id",
    "round_idx",
    "input_len",
    "output_len",
    "prefix_len",
    "tool_wait_after_ms",
]
CANON_FIELDS = [
    "request_id",
    "session_id",
    "round_idx",
    "arrival_time_ms",
    "prefix_len",
    "input_len",
    "output_len",
    "tool_wait_after_ms",
]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def as_int(v, field):
    if v is None or isinstance(v, bool):
        raise ValueError(f"invalid {field}: {v!r}")
    x = float(v)
    if not math.isfinite(x):
        raise ValueError(f"invalid {field}: {v!r}")
    return int(x)


def load_sessions(path):
    """Return list of sessions (first-appearance order); each a list of round dicts
    sorted by (round_index, line number)."""
    order, by_id, user_of = [], {}, {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line_no, line in enumerate(f):
            if not line.strip():
                continue
            r = json.loads(line)
            sid = r["session_id"]
            if sid not in by_id:
                by_id[sid] = []
                order.append(sid)
                user_of[sid] = r["user"]
            wait = 0.0
            for t in r.get("tools") or []:
                v = t.get("tool_wall_latency_ms")
                if v is None or isinstance(v, bool):
                    continue
                v = float(v)
                if math.isfinite(v) and v >= 0:
                    wait += v
            raw_new = as_int(r["newly_append_tokens"], "newly_append_tokens")
            raw_pre = as_int(r["prefix_tokens"], "prefix_tokens")
            raw_out = as_int(r["output_tokens"], "output_tokens")
            by_id[sid].append(
                {
                    "key": (as_int(r["round_index"], "round_index"), line_no),
                    "input_len": max(raw_new, 1),
                    "prefix_len": max(raw_pre, 0),
                    "output_len": max(raw_out, 1),
                    "peak": raw_pre + raw_new + raw_out,
                    "wait": wait,
                }
            )
            if user_of[sid] != r["user"]:
                raise ValueError(f"session {sid} has more than one user")
    sessions = []
    for sid in order:
        rounds = sorted(by_id[sid], key=lambda x: x["key"])
        for r in rounds:
            r["user"] = user_of[sid]
        sessions.append(rounds)
    return sessions


def pct(sorted_vals, q):
    """Nearest-rank percentile."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, math.ceil(q / 100.0 * len(sorted_vals)) - 1))
    return sorted_vals[k]


def dist(vals):
    s = sorted(vals)
    return {"p50": pct(s, 50), "p90": pct(s, 90), "max": s[-1] if s else None}


def make_split(eligible, seed, holdout_frac, max_context, in_sha):
    """Assign every user to holdout or dev. Users are visited in seeded random order and
    added to holdout until holdout's eligible sessions >= holdout_frac of all eligible."""
    per_user = {}
    for s in eligible:
        per_user[s[0]["user"]] = per_user.get(s[0]["user"], 0) + 1
    users = sorted(per_user)
    total = len(eligible)
    order = list(users)
    random.Random(seed).shuffle(order)
    holdout, n_hold = [], 0
    for u in order:
        if n_hold >= holdout_frac * total:
            break
        holdout.append(u)
        n_hold += per_user[u]
    hs = set(holdout)
    dev = [u for u in users if u not in hs]
    assert not hs & set(dev) and hs | set(dev) == set(users)
    return {
        "split_seed": seed,
        "holdout_frac_min": holdout_frac,
        "max_context": max_context,
        "input_sha256": in_sha,
        "holdout_users": sorted(holdout),
        "dev_users": dev,
        "eligible_sessions": {"holdout": n_hold, "dev": total - n_hold, "total": total},
        "users_with_eligible_sessions": {"holdout": len(holdout), "dev": len(dev)},
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--input", required=True)
    ap.add_argument("--out-dir")
    ap.add_argument("--sessions", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--start-min", type=float, default=0.25)
    ap.add_argument("--start-max", type=float, default=0.75)
    ap.add_argument("--max-context", type=int, default=262144)
    ap.add_argument("--max-tool-wait-ms", type=float, default=60000)
    ap.add_argument("--tracegen")
    ap.add_argument(
        "--make-split",
        metavar="FILE",
        help="write a user split JSON (needs --split-seed) and exit; no trace is built",
    )
    ap.add_argument("--split-seed", type=int, help="private seed for --make-split")
    ap.add_argument(
        "--holdout-frac",
        type=float,
        default=0.2,
        help="holdout users are added until they hold >= this share of eligible sessions",
    )
    ap.add_argument("--split-file", help="split JSON from --make-split; use with --side")
    ap.add_argument("--side", choices=["dev", "holdout"], help="sample only this side's users")
    a = ap.parse_args()
    if a.make_split:
        if a.split_seed is None:
            ap.error("--make-split needs --split-seed")
    else:
        if not a.out_dir or not a.tracegen:
            ap.error("--out-dir and --tracegen are required")
        if bool(a.split_file) != bool(a.side):
            ap.error("--split-file and --side go together")
    if not (0.0 <= a.start_min <= a.start_max <= 1.0):
        ap.error("need 0 <= start-min <= start-max <= 1")
    if a.sessions < 1:
        ap.error("--sessions must be >= 1")

    in_sha = sha256_file(a.input)
    if in_sha != EXPECTED_SHA256:
        sys.exit(f"input sha256 mismatch: {in_sha} (expected {EXPECTED_SHA256})")

    all_sessions = load_sessions(a.input)
    eligible = [s for s in all_sessions if max(r["peak"] for r in s) <= a.max_context]
    n_rounds_all = sum(len(s) for s in all_sessions)
    print(
        f"sessions={len(all_sessions)} rounds={n_rounds_all} eligible={len(eligible)}",
        file=sys.stderr,
    )

    if a.make_split:
        sp = make_split(eligible, a.split_seed, a.holdout_frac, a.max_context, in_sha)
        with open(a.make_split, "w") as f:
            json.dump(sp, f, indent=2, sort_keys=True)
            f.write("\n")
        print(
            f"split written: users holdout/dev = {len(sp['holdout_users'])}/{len(sp['dev_users'])}, "
            f"eligible sessions holdout/dev = {sp['eligible_sessions']['holdout']}/"
            f"{sp['eligible_sessions']['dev']}",
            file=sys.stderr,
        )
        return

    n_eligible_all = len(eligible)
    split_info = None
    if a.split_file:
        with open(a.split_file) as f:
            sp = json.load(f)
        if sp["input_sha256"] != in_sha or sp["max_context"] != a.max_context:
            sys.exit("split file does not match input sha256 / max-context")
        hu, du = set(sp["holdout_users"]), set(sp["dev_users"])
        assert not hu & du, "user on both sides"
        assert {s[0]["user"] for s in eligible} <= hu | du, "user missing from split"
        dev_pool = [s for s in eligible if s[0]["user"] in du]
        hold_pool = [s for s in eligible if s[0]["user"] in hu]
        assert len(dev_pool) + len(hold_pool) == len(eligible)
        assert not {id(s) for s in dev_pool} & {id(s) for s in hold_pool}, "session on both sides"
        assert not {s[0]["user"] for s in dev_pool} & {s[0]["user"] for s in hold_pool}
        pool = dev_pool if a.side == "dev" else hold_pool
        other = hold_pool if a.side == "dev" else dev_pool
        split_info = {
            "side": a.side,
            "other_side_exists": True,
            "users_on_this_side": len({s[0]["user"] for s in pool}),
            "users_excluded": len({s[0]["user"] for s in other}),
            "eligible_sessions_on_this_side": len(pool),
            "eligible_sessions_excluded": len(other),
        }
        eligible = pool
        print(
            f"side={a.side} users={split_info['users_on_this_side']} eligible={len(eligible)}",
            file=sys.stderr,
        )

    rng = random.Random(a.seed)
    sampled = []  # list of (source_index, start, rounds)
    for _ in range(a.sessions):
        si = rng.randrange(len(eligible))
        src = eligible[si]
        u = rng.uniform(a.start_min, a.start_max)
        start = min(int(u * len(src)), len(src) - 1)  # keep at least one round
        sampled.append((si, start, src[start:]))

    a.out_dir = os.path.abspath(a.out_dir)
    a.tracegen = os.path.abspath(a.tracegen)
    os.makedirs(a.out_dir, exist_ok=True)
    raw_csv = os.path.join(a.out_dir, "session_rounds_v2.csv")
    with open(raw_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(RAW_FIELDS)
        for new_id, (_si, _start, rounds) in enumerate(sampled):
            last = len(rounds) - 1
            for i, r in enumerate(rounds):
                wait = 0.0 if i == last else min(r["wait"], a.max_tool_wait_ms)
                w.writerow(
                    [new_id, i, r["input_len"], r["output_len"], r["prefix_len"], f"{wait:.6f}"]
                )

    out_csv = os.path.join(a.out_dir, "trace.csv")
    cmd = [
        a.tracegen,
        "coding-session",
        "--source",
        raw_csv,
        "--source-schema",
        "session-rounds-v2",
        "--out",
        out_csv,
        "--seed",
        str(a.seed),
        "--session-order",
        "source",
    ]
    print("running: " + " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, check=True)

    # Locate tracegen's manifest and plan (written beside the CSV).
    beside = sorted(
        n
        for n in os.listdir(a.out_dir)
        if n not in ("trace.csv", "session_rounds_v2.csv", "build_manifest.json")
    )
    tg_manifest = next((n for n in beside if "manifest" in n), None)
    tg_plan = next((n for n in beside if "plan" in n), None)
    tg_record = {}
    if tg_manifest:
        with open(os.path.join(a.out_dir, tg_manifest)) as f:
            tg_record = json.load(f)

    # Validate the canonical CSV and gather counts from it.
    sess = {}
    out_tokens = append_tokens = 0
    n_rows = 0
    with open(out_csv, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        if rd.fieldnames != CANON_FIELDS:
            sys.exit(f"unexpected canonical columns: {rd.fieldnames!r}")
        for row in rd:
            n_rows += 1
            sid, ri = int(row["session_id"]), int(row["round_idx"])
            if row["request_id"] != f"session_{sid}_round_{ri:06d}":
                sys.exit("bad request_id: {}".format(row["request_id"]))
            pre, inp, out = int(row["prefix_len"]), int(row["input_len"]), int(row["output_len"])
            out_tokens += out
            append_tokens += inp
            d = sess.setdefault(sid, [0, 0])
            d[0] += 1
            d[1] = max(d[1], pre + inp + out)
    if sorted(sess) != list(range(a.sessions)):
        sys.exit("session ids are not contiguous 0..N-1")

    tg_params = tg_record.get("parameters", {})
    folded = tg_params.get("folded_prefix_tokens")
    src_rounds_dropped = sum(
        si_len for si_len in (len(eligible[si]) - len(r) for si, _s, r in sampled)
    )

    users_used = {eligible[si][0]["user"] for si, _s, _r in sampled}
    manifest = {
        "input": {"path": os.path.abspath(a.input), "sha256": in_sha},
        "parameters": {
            "sessions": a.sessions,
            "seed": a.seed,
            "start_min": a.start_min,
            "start_max": a.start_max,
            "max_context": a.max_context,
            "max_tool_wait_ms": a.max_tool_wait_ms,
            "tracegen": os.path.abspath(a.tracegen),
            "tracegen_args": cmd[2:],
            "sampling": "with replacement over context-eligible sessions; session grouping by session_id; "
            "start = min(int(u*n_rounds), n_rounds-1), u~Uniform[start_min,start_max]",
        },
        "source_pool": {
            "sessions_total": len(all_sessions),
            "rounds_total": n_rounds_all,
            "sessions_eligible": n_eligible_all,
            "sessions_dropped_over_max_context": len(all_sessions) - n_eligible_all,
        },
        "counts": {
            "sessions": len(sess),
            "rounds": n_rows,
            "output_tokens": out_tokens,
            "appended_tokens": append_tokens,
            "folded_prefix_tokens": folded,
            "folded_first_rounds": tg_params.get("folded_first_rounds"),
            "folded_rounds": tg_params.get("folded_rounds"),
            "major_compaction_rounds": tg_params.get("major_compaction_rounds"),
            "source_rounds_dropped_before_start": src_rounds_dropped,
            "distinct_source_sessions": len({si for si, _s, _r in sampled}),
            "distinct_users": len(users_used),
        },
        "split": split_info,
        "session_rounds": dist([v[0] for v in sess.values()]),
        "peak_context": dist([v[1] for v in sess.values()]),
        "peak_context_note": "max over rounds of prefix_len+input_len+output_len in the emitted canonical CSV",
        "outputs": {
            "session_rounds_csv": raw_csv,
            "session_rounds_csv_sha256": sha256_file(raw_csv),
            "canonical_csv": out_csv,
            "canonical_csv_sha256": sha256_file(out_csv),
            "canonical_csv_bytes": os.path.getsize(out_csv),
            "tracegen_manifest": os.path.join(a.out_dir, tg_manifest) if tg_manifest else None,
            "tracegen_plan": os.path.join(a.out_dir, tg_plan) if tg_plan else None,
        },
    }
    with open(os.path.join(a.out_dir, "build_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
