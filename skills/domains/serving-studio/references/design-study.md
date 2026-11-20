# A design study

A what-if (step 4) asks about one change. A design study asks what the system should be: which
combination of system-level choices serves this model, hardware and workload best, how much of
that today's engine reaches, and in what order to close the gap. Its result is a global plan:
a ladder of changes from today's engine to the best design, each step with its simulated gain.
Run it at the start of an optimization, or when a calibration moves enough to change the
ranking, on a local checkout ([local-sim.md](local-sim.md)): it needs sweeps and overrides,
which the hosted service does not run.

## 1. Enumerate every axis the preset exposes

Run `list-params --human` at your revision and list every system-level axis the preset's
architecture and worker take: parallel layout (data, tensor, expert parallel widths and how
they map onto devices), collectives and MoE dispatch, padding across parallel groups (SPMD),
prefill, decode and context buckets, chunk size (the batch token budget), region granularity,
admission and scheduling (chunks per step, placement across groups, batch limits), speculative
depth, and disaggregation. For each, write the values the engine could plausibly run, not only
the one it runs today.

No axis is inherited silently from the engine. An axis held at the engine's value (its padding,
its minimum bucket, its layout) is an **assumption**: name it, say why it is held (the engine
cannot change it within this task, the hardware forbids another value), and expect it to hide
part of the design space. In the Trainium campaign, taking the engine's padding, minimum bucket
and layout as fixed hid most of the design space.

## 2. Sweep at several kernel-efficiency scenarios

Sweep the axes (one stage over the layout and the large switches, then one knob at a time
around the winners) under at least two kernel scenarios: **today's kernels** (the calibrated
model when there is one, [calibration.md](calibration.md), otherwise the uncalibrated preset)
and one or more scenarios in which a kernel kind is faster by a stated factor (`kind_time_scale`
0.5 on MoE prefill, say). A design that wins only once a kernel is faster says which kernel work
must come first; one that wins under every scenario is robust. Scenario results are bounds, not
predictions: no one has built the faster kernel.

Run at the benchmark's workload, at each concurrency the task's objective scores. Keep the
command and the result directory of every run.

## 3. Build the cumulative ladder

Start from today's engine (the head's preset, reproduced in step 2 of the skill) and add one
change at a time, each step keeping every earlier one, ordered by gain per unit of work
(cheapest large gains first), until the best design. Each step records its overrides
(cumulative), the metric at each concurrency, its command and its result directory. The ladder
is the global plan: each step is a candidate plan for the planners, and its gain over the step
before is the simulated gain that plan would claim. A step whose gain is within the model's
residual is not a reason on its own.

## 4. Record the best design per concurrency

For each concurrency, the best design found, as overrides of today's preset, with its metric,
command and result directory. Say "best of those evaluated": a design space the study did not
enumerate is not covered.

## Record

Keep the study with the wrapper; it refuses a study that sweeps no axis, has an assumption with
more than one value, lacks a today's-kernels scenario, starts its ladder anywhere but today's
engine, or drops a change on the way up:

```bash
python3 <skill-dir>/scripts/servingstudio.py record --kind study --file study.json
```

```json
{
 "simulator": {"checkout": "<value of SERVINGSTUDIO_SIM>", "revision": "<checkout HEAD>"},
 "preset": "<today's engine preset, under the records directory's presets/>",
 "workload": "<the benchmark's workload>",
 "metric": "<the task's metric>",
 "calibration": "<the calibration id the today's-kernels runs used, or null>",
 "axes": [{"name": "<override path>", "values": [512, 2048, 4096], "assumption": null},
          {"name": "<override path>", "values": [true], "assumption": "<why it is held>"}],
 "scenarios": [{"name": "today", "overrides": {}},
               {"name": "moe_prefill_2x", "overrides": {"<kind time scale path>": 0.5}}],
 "ladder": [{"name": "engine", "overrides": {}, "values": {"64": 194.0},
             "command": "<command>", "result_dir": "<dir>"},
            {"name": "+ chunk 512", "overrides": {"<path>": 512}, "values": {"64": 240.0},
             "command": "<command>", "result_dir": "<dir>"}],
 "best": [{"concurrency": 64, "overrides": {"<path>": 512}, "value": 240.0,
           "command": "<command>", "result_dir": "<dir>"}]
}
```

`values` maps each concurrency to the metric there. Studies live beside the calibrations, as
`study-NNN.json` ([calibration.md](calibration.md#where-records-live)); a new study is a new
record, never an edit.

## Handing it on

A planner that ran the study writes its id and the ladder in the plan it proposes; each step
that becomes its own plan freezes its own Prediction ([prediction.md](prediction.md)) for that
step alone, against the head it will be measured on, not the ladder's cumulative number. The
ladder is redone when the head moves enough that its first steps are done or its order changes.
