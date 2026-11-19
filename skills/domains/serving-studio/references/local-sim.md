# A local ServingStudioSim checkout

Use it when the API does not serve the deployment (Trainium, an unpublished model) or when a
question needs what-if overrides. `$SERVINGSTUDIO_SIM` is the checkout; run every command from
its root through `uv run --no-sync` (plain `uv run` may install or update packages), and write
outputs under your scratch directory: the checkout is shared with other agents, and the
repository you optimize is not the place for them either.

## Run

```bash
cd "$SERVINGSTUDIO_SIM"
uv run --no-sync python -m launcher <preset.yaml|json> --no-gpu --dry-run      # validate, print the configs
uv run --no-sync python -m launcher <preset.yaml|json> --no-gpu \
  --override io.log_dir=<scratch>/sim-<name>                          # run and analyze
uv run --no-sync python -m launcher list-params --human                          # every field a preset takes
uv run --no-sync python -m launcher timing-predict <config> --no-gpu             # one batch, no scheduler
```

`--no-gpu` guarantees nothing touches an accelerator; a step that would need one (a missing
kernel row) fails and names itself instead. `--override path=value` patches one field of the
expanded preset by its dotted path, and a list of values in a preset sweeps it. Read
`launcher/README.md` in the checkout for sweeps, and `analyzer/README.md` for the reports.

Each run directory holds `summary.json` (did every request finish), `raw/` (requests, kernel
costs, the resolved configuration), `reports/` (throughput, latency, time attribution),
`payloads/` and `plots/`. Quote numbers from `reports/` and `summary.json` with the directory.

## Start from the engine, not from a default

Copy the preset nearest the task under `presets/` into your scratch directory and change it to
what the engine runs: parallel layout, buckets and chunk sizes, the scheduler's batching limits,
speculative depth with the acceptance the engine measured, and the benchmark's workload as a
trace (fixed arrivals for a burst, the recorded arrivals for a replay). Keep the file: it is
part of the provenance of every number it produces.

## What-ifs

A what-if is the same preset with one override, run beside the reproduction:

- **A configuration** the engine can run (another layout, chunk size or depth): a prediction.
- **A cost scale** on one kernel kind or phase (the Trainium campaign used `kind_time_scale` on
  the MoE prefill kinds: 0.75x, 0.5x, 0.33x): a bound on what a faster kernel could give, never
  a prediction of a kernel nobody has built. `list-params --human` names the scales your
  revision offers.

Sweep a scale over several factors and report the curve: a gain that flattens early says the
next bottleneck is close.

The same scales, fitted to the head's measured timeline instead of chosen, are a calibration
([calibration.md](calibration.md)): a calibrated what-if runs with the calibration's factors as
overrides on both the reproduction and the change. A sweep over many system axes at once is a
design study ([design-study.md](design-study.md)).

## Trainium

Trainium2 needs the `[trainium]` commit of [`../assets/pins.toml`](../assets/pins.toml)
(ServingStudioSim pull request 107), unless the task pins its own. Its presets are under
`presets/trn2/`, and
`doc/pr/trn2-flashnext/` in that checkout records how the model was aligned with a real engine
and which parts it does not trust: the all-to-all cost at 8-way data parallelism, activation and
scratch memory in the HBM check, and first-use stalls after warm-up. A prediction that rests on
one of those parts says so.
