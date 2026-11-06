---
name: visualize-progress
description: "Use when you need charts of how a vibesys optimization is going: the headline objective along the result branch, the throughput-versus-latency Pareto frontier, latency along the branch, and how plans ended. Reads the optimization state and writes PNGs plus a report.md. Read-only on state; runs no benchmark."
---

# Visualize optimization progress

One script turns what `vibesys-cli` reports about the task into four charts and a table. Use it for
a report, a stop, or when someone asks how far the optimization got. It runs `vibesys-cli jobs`,
`show` and `plan list` with `--json` and reads the result branch's history from git; it changes no
state and runs no benchmark.

## Run

```
uv run <skill>/scripts/plot_progress.py --branch vibesys/<task> --repo . --out <dir> [--theme dark]
```

`uv` supplies matplotlib from the script's inline metadata; with plain Python (3.11 or newer) install
`matplotlib>=3.8` first. The CLI is `$VIBESYS_CLI` when set, else `vibesys-cli` on the path; `--cli`
names another. The script uses the numbers the CLI computed (headline value, objectives, noise) and
repairs nothing: if the CLI reports an error, fix that, not the script. A task with a single metric
gets the progress and plan charts only.

## Output

| File | What it shows |
|------|---------------|
| `progress.png` | every measured experiment in run order (dot = sample mean, bar = min to max; filled = on the result branch, gray ring = not) with the best promoted result so far as a line (not-promoted dots never move it) |
| `frontier.png` | headline versus each latency objective; orange = Pareto frontier, blue = promoted, gray = not promoted |
| `latency.png` | each latency objective along the result branch; only heads measured with that objective appear |
| `plans.png` | plans per status |
| `report.md` | a table of the heads on the result branch (value, change, label) and the images |

## Read them honestly

1. Every point is the CLI's value for the commit's newest measurement (bars span its samples); say how many in the caption of
   anything you share. A change inside the bars is not a result.
2. Heads from different hardware or hosts are not comparable. Split the chart or say so when a
   branch crosses node types.
3. The frontier uses the task's noise tolerance: a point is dropped only when another point is no
   worse on every objective and better beyond the tolerance on at least one. Two points inside
   the tolerance both stay.
4. Candidates are not verified wins. Never call a hollow dot an improvement; promotion needs a
   judge pass and a recorded `better` (see `vibesys:optimize`).

## Rules

- Do not hand-edit the images or the numbers; fix the cause instead.
- Render one image and look at it before sharing: the script checks data, not layout.
- Tests: from this skill's folder, `pytest --noconftest -c /dev/null --rootdir . tests`.
