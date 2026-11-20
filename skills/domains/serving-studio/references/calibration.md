# A calibrated model

An uncalibrated simulator reproduces the head with a residual (step 2) and every number after it
carries that residual. A calibrated model goes further: a few factors fitted to the head's
measured timeline, so that the simulator's per-phase and per-kernel times match what the
engine spent. A Trainium campaign re-fitted after every official measurement, and its
calibrated predictions then landed within a few percent of the next one. This page is how to
fit one, where it is kept, and how a prediction names it.

Calibration needs a **match** (step 1) on a **local checkout**: the factors are launcher
overrides, which the hosted service does not take, and a proxy's residual measures the
hardware difference too, which no factor should absorb. A proxy and a bound stay uncalibrated.

## The direction rule

A measurement changes what the simulator says about the world only when **an external fact**
changed or was measured: the hardware, the model, the workload, or a kernel's measured cost on
this hardware. **A limitation of the current engine is never copied into the simulator.**
Padding the engine does that a better engine would not, a minimum bucket, a missing overlap, a
host stall after warm-up, a slow path that a fix removes: these go into the head's preset as
the engine's configuration (a design axis a study can change, [design-study.md](design-study.md)),
or stay in the residual, named. They never go into a factor or the simulator's defaults, because
a simulator that has learned today's engine can only predict today's engine, and every what-if
then inherits the limitation it was meant to remove.

So, for each disagreement the timeline shows, ask which it is:

| The timeline shows | What changes |
| --- | --- |
| A kernel kind runs slower or faster than its profiled rows say, at the shapes the engine ran | that kind's time scale (a fitted factor) |
| The engine pads, buckets, splits or schedules differently from the preset | the preset, to say what the engine does; no factor |
| A cost the simulator does not model (host scheduling, first-use stalls, tokenizer) | nothing: it stays in the residual, listed in `engine_limitations_not_fitted` |
| The hardware, model or workload is not the one the preset describes | the preset or the workload; if the simulator cannot express it, the deployment is a proxy and is not calibrated |

## Fit

After each **official measurement** of the head (the base's measure at the start, and the
measure of each promoted candidate, which becomes the head), and only with its timeline:

1. Take the newest record (below) and simulate the measured commit with its factors: the
   residual of that run against the new measurement is the new record's `residual_before`. It
   is a held-out error, the previous model's real miss, and the number to quote when asked how
   good the calibrated model is. With no earlier record, simulate the uncalibrated preset.
2. Read the measured timeline: per step, the phase, its batch shape and its wall time (the
   engine's step log or a profile of the measured commit, from `profile-revision`). An
   end-to-end number alone does not say which factor is off: without a timeline, do not
   calibrate.
3. Fit as few factors as explain the timeline, each one a launcher override that
   `list-params --human` lists at your revision: a time scale per kernel kind that disagrees, a
   decode factor, a prefill factor. Fit them on the phase totals the timeline shows (decode
   step time at each batch size, prefill forward time at each chunk), not on the end-to-end
   metric, so a factor cannot absorb a scheduling difference. Apply the direction rule to each.
4. Simulate again with the fitted factors: the residual is `residual_after`. It is in-sample,
   so it says the fit worked, not that the next prediction will hold.
5. Keep the record with the wrapper. It checks the record's format, writes it as the next
   number and never replaces an earlier one:

   ```bash
   python3 <skill-dir>/scripts/servingstudio.py record --kind calibration --file cal.json
   ```

Re-fitting is never editing: each official measurement adds a record that names the one it
supersedes, and earlier records stay as they were, so a frozen prediction's calibration can
always be read back.

## Where records live

In `SERVINGSTUDIO_RECORDS`, one directory per task, outside every repository and never under
`.vibesys/`: `<parent>/<name>.servingstudio/<task>/`, beside the worktrees directory that
`vibesys:state` describes (for a repository at `/work/kv`, `/work/kv.servingstudio/<task>/`).
The lead creates it once, names it in every brief that loads this skill, and does not remove it
when the run ends: the records outlive the run, and the next run of the task starts from its
newest calibration. Records are `calibration-NNN.json` and `study-NNN.json`, numbered from 001
in the order they were kept; the id is the name without `.json`. Copy each preset file a record
names into `presets/` there, since a scratch directory may be cleared.

## Record format

```json
{
 "simulator": {"checkout": "<value of SERVINGSTUDIO_SIM>", "revision": "<checkout HEAD>",
               "pinned_by": "vibesys.input.toml | pins.toml"},
 "preset": "<the preset file, under the records directory's presets/>",
 "workload": "<the benchmark's workload, as the Prediction block's Workload line>",
 "fitted_on": [{"commit": "<measured commit>", "job": "<measure job id>",
                "metric": "<metric>", "measured": 194.0,
                "timeline": "<the step log or profile the factors were fitted on>"}],
 "factors": {"<override path>": 1.12},
 "residual_before": {"<metric>": 0.18},
 "residual_after": {"<metric>": -0.02},
 "engine_limitations_not_fitted": ["<a disagreement left in the residual, and why>"],
 "commands": ["<each simulator command the fit ran>"],
 "result_dirs": ["<each result directory, the before and after runs first>"],
 "supersedes": "<the previous calibration's id, or null for the first>"
}
```

Residuals are signed fractions, (simulated - measured) / measured, one per metric of
`fitted_on`. `pinned_by` says which pin chose the revision (Setup); leave it out when the
checkout is at neither. The wrapper refuses a missing or unknown key, a factor that is not a
positive number, a residual missing for a fitted metric, and a `supersedes` that is not the
newest calibration in the directory, naming the key.

## Using it

A calibrated model is the preset plus the record's factors as overrides. Every run made with it
names the record id in its Prediction block's `Model` line ([prediction.md](prediction.md)).
A calibration fitted at another simulator revision, or on another hardware, model or workload,
does not apply: fit a new one. When the next official measurement comes, its `residual_before`
is how the predictions made from this record should be judged.
