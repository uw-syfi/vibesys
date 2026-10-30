---
name: dial9-rank-bottlenecks
description: >-
  Rank where a dial9-instrumented Tokio engine (for example RisingWave) spends
  its in-window on-CPU time, and read the slow-versus-starved verdict. This is
  the entry workflow over the vendored dial9 trace skills and the on-CPU ranker
  in this bundle. Activate when you have a dial9 trace of an async Rust database
  or streaming engine and need to know which operator or task is the
  bottleneck, whether it is CPU-bound or starved waiting on an upstream
  operator, and where to drill next.
---

# dial9-rank-bottlenecks

Detection only. This skill answers one question about a running Tokio engine:
which component burns the most on-CPU time in the steady-state window, and is it
slow (CPU-bound) or starved (waiting off-CPU). It does not change the engine and
does not score it; the scored metric stays the workload's throughput. The
ranking only says where the time goes.

It is the front door to this bundle. Run the ranker to get a verdict, read the
verdict with the rules below, then hand off to the vendored `dial9-*` skills to
root-cause a specific finding. The ranker and those skills are siblings here, in
`skills/domains/dial9-tokio/`.

## 1. Produce the ranking

One command. The ranker parses the trace, claims on-CPU nanoseconds to the
innermost covering span per task, and writes profile result protocol v1:

```
python3 skills/domains/dial9-tokio/ranker/analyze_dial9.py \
    --traces <trace-dir-or-file> \
    --window <workload-driver.json> \
    --vs-output <out.json>
```

It finds the vendored toolkit automatically; `--toolkit` / `DIAL9_TOOLKIT` only
override it. `--window` is the workload driver's JSON carrying the steady-state
window, so startup and ramp never enter the ranking; omit it only to rank the
whole trace, which the summary then flags as "startup and ramp included".

The exit code is the contract: **0** means a ranking was written; **2** means a
setup or tool error and NO verdict (see section 4). A degraded ranking is never
emitted, because a wrong answer about where time goes costs more than no answer.

## 2. Read the verdict

Cost is in-window on-CPU poll seconds per component; share is that over the
in-window on-CPU total. The top row is the CPU bottleneck. The stdout summary
prints the table and `Top bottleneck: <name> (<share>)`; the `--vs-output` JSON
carries the same per-component `cost`, `share`, and `evidence` lines.

Shares sum to at most 1. The shortfall is `unattributed` poll time: poll time no
span covered.

## 3. Slow versus starved, the reading that matters

The `evidence` lines are the point. A CPU-only or sampling profiler cannot make
this distinction; dial9 can, because it records both wall-clock and on-CPU time
per span.

| Evidence line | Means | What to do |
| --- | --- | --- |
| `<n>s on-CPU self time across <k> spans on <t> tasks`, with a high share | CPU-bound: the component is computing | This is the real bottleneck; attack it |
| `... off-CPU (blocked or starved)` with a high percent | Starved: elapsed time dwarfs on-CPU time, so it is waiting, usually on an upstream operator | Do not attack it; attack what it waits on |
| `... including nested spans, so X% of its time is inside instrumented children` | Wrapper: its own code is cheap, its children hold the time | Attack the children, not the wrapper |
| `run: ... mean worker utilization ... sched delay ... queue depth ...` | Run-level context, repeated on every component | See below |

Run-level context separates operator-bound from runtime-bound. Low mean worker
utilization with one component dominating means a single operator that does not
parallelize is the limit, not runtime saturation. High scheduler delay or deep
queues mean scheduling pressure.

Worked example, a real nexmark q7 run: `fragment 2 StreamHashJoin` ranks first at
roughly 67 to 75% of on-CPU poll time (CPU-bound), while every downstream
`fragment N StreamProject` carries a `98% off-CPU` line (starved, waiting on the
join). Mean worker utilization of 16 to 32%, with the join about 7x the next
component, says the limit is the join's single-threaded cost, not the runtime.
The target is the join; optimizing a starved Project would change nothing.

## 4. When the exit code is 2

The traces cannot support a trustworthy ranking. The error names every reason at
once. Fix the build or the capture, do not work around it:

| Reason named in the error | Fix |
| --- | --- |
| `missing-frame-pointers` | build with `-C force-frame-pointers=yes` |
| `no-tokio-unstable` | build with `--cfg tokio_unstable`; without it, poll events cover only dial9's own spawns and every cost is over a biased subset |
| no spans, so no component can be named | attach `Dial9TracingLayer` so the engine's per-component tracing spans are recorded |
| task coverage is dial9-spawns-only | route root futures through `dial9::block_on` / `dial9::spawn` |
| unresolved spans, or no in-window poll time | check the window's timebase and that the window overlaps real activity |

Re-run after fixing. Do not lower the bar to get a number.

## 5. Drill into a finding

This skill ranks and reads; the vendored skills in this bundle explain and
root-cause. Hand off:

- Why the hot component's polls are long, or why a starved one waits:
  `dial9-diagnose-long-poll`, `dial9-red-flags`.
- The Tokio execution model behind the numbers (waking, parking, cooperative
  scheduling, tail latency): `dial9-runtime`.
- Expand one timestamp into a per-worker, per-thread view: `dial9-zoom-window`.
- Parse or re-analyze the trace directly: `dial9-toolkit`,
  `dial9-trace-loading`, `dial9-trace-analysis`, `dial9-trace-recipes`.
