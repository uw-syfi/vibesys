---
name: visualize-progress
description: "Use when you need charts of how a vibesys optimization is going: the headline objective along the result branch, the throughput-versus-latency Pareto frontier, latency along the branch, and how plans ended. Reads the optimization state and writes PNGs plus a report.md. Read-only on state; runs no benchmark."
---

# Visualize optimization progress

One script turns `.vibesys-state/optimize/<task>/` into four charts and a table. Use it for a
report, a stop, or when someone asks how far the optimization got. It reads only
`measurements/*.jsonl` and `plans/*.md`; it changes no state and runs no benchmark.

## Run

```
uv run <skill>/scripts/plot_progress.py --state .vibesys-state/optimize/<task> \
  --task-dir .vibesys/tasks/<task> --branch vibesys/<task> --repo . --out <dir> [--theme dark]
```

`uv` supplies matplotlib from the script's inline metadata; with plain Python install
`matplotlib>=3.8` first. The inputs must be well formed, as `vibesys:state` and `vibesys:measure`
define them (full-SHA records with `recorded_at`, `label`, `check`, `samples`; single-word plan
`status:`; `objectives.toml` with a `[pareto]` table). The script repairs nothing and fails on
malformed input: fix the state, not the script.

## Output

| File | What it shows |
|------|---------------|
| `progress.png` | every measured experiment in run order (dot = sample mean, bar = min to max; filled = on the result branch, gray ring = not) with the best promoted result so far as a line (not-promoted dots never move it) |
| `frontier.png` | headline versus each latency objective; orange = Pareto frontier, blue = promoted, gray = not promoted |
| `latency.png` | each latency objective along the result branch; only heads recorded with `--observations` appear |
| `plans.png` | plans per status |
| `report.md` | a table of the heads on the result branch (value, change, label) and the images |

## Read them honestly

1. Every point is the mean of the samples recorded for that commit; say how many in the caption of
   anything you share. A change inside the bars is not a result.
2. Heads from different hardware or hosts are not comparable. Records carry `host`; split the
   chart or say so when a branch crosses node types.
3. The frontier uses the task's noise tolerance: a point is dropped only when another point is no
   worse on every objective and better beyond the tolerance on at least one. Two points inside
   the tolerance both stay.
4. Candidates are not verified wins. Never call a hollow dot an improvement; promotion needs a
   judge pass and a recorded `better` (see `vibesys:optimize`).

## Rules

- Do not hand-edit the images or the numbers; fix the state instead.
- Render one image and look at it before sharing: the script checks data, not layout.
- Tests: from this skill's folder, `pytest --noconftest -c /dev/null --rootdir . tests`.
