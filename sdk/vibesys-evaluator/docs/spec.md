# vibesys-evaluator: design spec

Status: draft (work package 1). Nothing here runs yet. The intended public
Python API is [`api/public-api.txt`](../api/public-api.txt), the schema is
[`loadgen.proto`](../../../proto/vibesys/evaluator/v1/loadgen.proto) and
[`phase.proto`](../../../proto/vibesys/evaluator/v1/phase.proto), and three
example tasks written against both are in [`examples/`](../examples).

## What it is

`vibesys-evaluator` is the package a task writer builds a benchmark on. A task
(see [the vibesys-cli task environment](../../../docs/contributing/cli-task-environment.md))
is a Dockerfile, `vibesys.input.toml`, `objectives.toml`, `OBJECTIVE.md`, the
application's deploy files and, with this package, two Python files:

- `bench.py`: (1) the system under test (build, deploy, readiness), (2) the
  initial data (seed), (3) the workload (operation functions and the model
  they are checked against), (4) a `benchmark(run)` function that is the
  scoring definition, (5) the validity rules.
- `dataset.py`: a deterministic generator of the initial data.

The Dockerfile is `FROM vibesys-evaluator:<ver>@sha256:...` plus
`COPY . /task` (and any client libraries the seed needs, such as pymongo).
One benchmark run is one sample; it writes result-protocol-2 records
([PROTOCOL.md](../../vs-evaluator/PROTOCOL.md)) to the path after
`--vs-output`.

The package has two parts in one image and one version: the Python package
`vibesys_evaluator`, and **loadgen**, a Go binary that sends the requests.
Microservices over HTTP on Kubernetes come first. The yardsticks are the
`hotel-k8s` and `social-network-k8s` tasks of the DeathStarBench fork,
rewritten on it with no code shared between them, each measuring within
noise of its current `run.py`.

## Decisions this spec builds on

Recorded with the user before this spec; not reopened here.

- No recipe concept: `benchmark(run)` composed from phase primitives
  (`run.closed_loop`, `run.open_loop`) is the scoring definition.
- Operations are functions `op(model, rand) -> Request` in a weighted mix.
  Responses are validated by per-operation functions, after the phase, over a
  sample (writes 100%, reads 10% by default).
- `python /task/bench.py` in the task container only orchestrates (deploy,
  phases, records, teardown). Each phase runs as an in-cluster pod of the task
  image. In it, Python writes the phase's requests to a file before the
  window, loadgen reads the whole file before t0, runs the phase and writes
  results (from a writer thread that never blocks senders) and a summary;
  Python validates after. No Python runs during the measured window.
- loadgen never draws randomness that changes what is sent; Poisson arrival
  times from a given seed are the one exception. Multi-step operations are
  templates loadgen fills from earlier responses.
- loadgen computes per-operation HDR latency histograms, send lag, its own
  CPU and whether the load was sustained. Latency is end minus scheduled
  (open loop) or end minus start (closed loop).
- Saturation guard: a sample fails when loadgen is the bottleneck.
- The schema is protobuf with protovalidate, length-delimited binary by
  default, proto3 JSON lines for debugging, a file header with a schema
  version, additive-only within v1.
- One version for the Python package, the Go module tag and the image.
- Public API rule: see [`AGENTS.md`](../AGENTS.md).

## Architecture

```text
 vibesys-cli ── docker run ──▶ task container (task image)
                               python /task/bench.py benchmark --vs-output F
                               ┌────────────────────────────────────────────┐
                               │ orchestrator (vibesys_evaluator.main)       │
                               │  hello record ─▶ F                          │
                               │  deploy.k8s: build images (buildctl), fresh │
                               │  namespace, data tier, seed Job, services,  │
                               │  readiness                                  │
                               │  benchmark(run): for each phase ─┐          │
                               │  result/error record ─▶ F        │          │
                               │  teardown (every exit path)      │          │
                               └──────────────────────────────────┼──────────┘
              kubectl cp PhaseInput (seeds, load, history) ──────▼
 ┌──────────────── phase pod (task image, in the namespace) ─────────────────┐
 │ python bench.py (phase mode)                                              │
 │  1 dataset() ─▶ model(data, rand) ─▶ replay history (apply)               │
 │  2 generate: pick op by weight, op.build(model, rand) ─▶ requests file    │
 │                (writes applied as pending while generating)               │
 │  3 exec loadgen open|closed ──▶ gateway Service ──▶ application pods      │
 │        reads the whole plan, t0, warmup, window; results + summary files  │
 │  4 rebuild model, replay history + this phase's writes (apply)            │
 │    check each sampled call in start order; saturation and validity        │
 │  5 PhaseOutput (summary, violations, new writes) ─▶ kubectl cp back       │
 └───────────────────────────────────────────────────────────────────────────┘
```

Dependencies point one way: `bench.py` imports `vibesys_evaluator`; the
package calls loadgen as a subprocess through files whose schema is the
`.proto`; loadgen knows nothing of operations, models or tasks.

## The runtime, phase by phase

### The sample (orchestrator)

1. Parse the command line: `benchmark --project-root DIR --vs-output FILE`
   (and `ctl ...`, see "Deploy layer"). Draw the sample seed; write and flush
   `hello` with the declared metrics.
2. Deploy (`deploy.k8s.App`): sweep this task's stale namespaces; build the
   candidate images and the in-cluster copy of the task image; create a fresh
   labelled namespace; apply the data tier and wait until Available; run the
   seed Job; apply the rest; wait until every Deployment has all replicas
   available; probe readiness.
3. Call `benchmark(run)`. Each `run.closed_loop` / `run.open_loop` is one
   phase pod; it returns a `PhaseResult` or raises, which ends the sample with
   an `error` record naming the phase and what failed.
4. `run.report(values)` checks the values against the declared metrics and
   writes the `result` record.
5. Delete the namespace on every exit path (return, exception, SIGTERM,
   SIGINT, SIGHUP). SIGKILL leaks it; the next sample's sweep deletes
   namespaces older than the longest a job may run plus a margin.

### A phase (phase pod)

1. **Model.** Generate the dataset, build `workload.model(data, rand)` with
   the sample's model stream (the same in every phase), and replay the
   sample's history: every write of the earlier phases, in start order, as a
   `Call` with its outcome and times, through the operation's `apply`. This
   is how state crosses phases: social network's open phases see the closed
   phases' composes and follow changes, and hotel's phases continue from the
   values the earlier ones wrote, so nothing is reset between phases.
2. **Generate.** Draw operations from the mix by weight with the phase stream;
   each gets its own child stream. `build(model, rand)` returns a `Request`.
   For a write (an operation with `apply`), `apply` runs at once with a
   pending `Call` (no times, `ok` None), so later operations of the same plan
   build on it (a follow, then an unfollow of that edge). Whether the
   operation is in the validation sample is drawn here too, so the sample does
   not depend on timing. Open loop: `ceil(1.02 rate (warmup + secs)) + 100`
   operations; closed loop: `max_ops_per_sec (warmup + secs)`.
3. **Run.** `loadgen` reads the whole plan into memory, then starts t0. It
   sends a prefix of the plan: clients take operations in plan order; at the
   window's end no new operation starts and those in flight complete, bounded
   by the request timeout. Operations never taken are never sent and do not
   enter the history.
4. **Check.** Rebuild the model from the history plus this phase's sent
   writes, then call `check(model, call)` for every sampled operation in order
   of start time (a model may learn from earlier responses, as social
   network's first sight of a live post does). Writes are always in the
   sample. A check returns `None` or what is wrong.
5. **Judge.** Apply the saturation guard and the validity rules; leave a
   `PhaseOutput`. The results file is kept as an artifact, compressed.

### Time windows

loadgen's clock in the pod is the only clock of a phase. With `W` the
warmup and `S` the window: the warmup is `[t0, t0+W)`, the measurement window
`[t0+W, t0+W+S)`. Latency, throughput, error rate and the validity rules use
the operations scheduled (open loop) or started (closed loop) in the window.
Throughput is the operations completed in the window divided by `S`;
operations started in the window that end after it are counted as `spilled`.
Checks run on sampled operations of both the warmup and the window; a failed
check in the window is an error, one in the warmup is reported only (as the
servicebench engine does today).

`Call.start` is when the first step was sent (after any wait for the
exclusive key) and `Call.end` when the last step's response was complete.
`Versions` (below) turns them into the visibility rule: a read may show, per
key, the latest value acknowledged more than `bound_secs` before it started
(the default if none) or any value whose write started before it ended; a
write that failed counts as possibly applied. Times are conservative, so
slack only favours the application. Times of different phases come from
different pods' clocks; the 1 s bound of both yardsticks is far larger than
the skew of a cluster's synchronized clocks.

## The Python API

Nineteen public names (listing: [`api/public-api.txt`](../api/public-api.txt)):

| Module | Names |
| --- | --- |
| `vibesys_evaluator` | `main`, `Run`, `PhaseResult`, `Metric`, `Validity`, `Rand`, `Versions`, `Call`, `Response` |
| `vibesys_evaluator.service` | `Workload`, `Operation`, `Request`, `Step`, `Template`, `get`, `post`, `put`, `ref` |
| `vibesys_evaluator.deploy.k8s` | `App` |

- `Operation(name, weight, build, check, apply, sample)`. `apply` makes an
  operation a write; `sample` defaults to 1.0 for writes and 0.1 otherwise,
  and a write may not set it lower.
- `Request(*steps, info, exclusive)`. Steps are sent in order on one client
  and form one operation with one latency. `info` (JSON) holds what the
  request was built from; it is not sent and comes back in the `Call`.
  Operations with the same `exclusive` key never overlap; loadgen grants the
  key in plan order, so writes to one item are applied in plan order.
- `get`/`post`/`put(path, *, query, form, json, body, headers, capture, expect)`.
  `capture={"token": "/token"}` takes a value from the step's response by
  JSON pointer; `ref("token")` puts it into a later step (`"Bearer " +
  ref("token")` is a `Template`). `expect` is the status (or statuses) that
  count as success for loadgen's error count; the default is 200.
- `Rand(seed, *labels)`: SplitMix64 streams with FNV-1a label mixing,
  `child`, `random`, `below`, `weighted`, `normal` (Irwin-Hall of four),
  `zipf(n, s)` (inverse CDF, cached per `(n, s)`). The algorithms are part of
  the versioned contract and equal the Go generators' `newRNG`/`Mix`, so a
  dataset is reproducible across versions and a port from Go can be exact.
- `Versions(bound_secs)`: `write(key, value, call)`, `latest(key, default)`
  (generation: pending writes included), `allowed(key, default, start=, end=)`
  (checks).
- `Run.closed_loop(clients, secs, warmup, max_ops_per_sec)`,
  `Run.open_loop(rate, secs, warmup, arrivals)`, `Run.report(values)`.
  `PhaseResult` has `ops_per_sec`, `p50_ms`, `p99_ms`.
- `main(dataset, system, workload, benchmark, metrics, validity)`.

Where the examples show the hard parts:

- Multi-step template: `examples/kv-store/bench.py` (`login`: log in, capture
  the token, send it); `examples/social-network-k8s/bench.py`
  (`compose_post`: compose, then read your write);
  `examples/hotel-k8s/bench.py` (`reserve_capacity`, `set_capacity`).
- Cross-phase state: `Model` and `apply_compose`/`apply_edge` in
  `examples/social-network-k8s/bench.py`; the history replay replaces the
  write journal, `--state-out`/`--state-in` and run.py's ConfigMaps.
- Writes checked within the 1 s bound: `apply_*` and `check_rates`,
  `check_features` in `examples/hotel-k8s/bench.py`.

## loadgen command line

```text
loadgen closed --target URL --clients N --secs S --warmup W
               --requests F --results F --summary F
               [--timeout S] [--format binpb|json]
loadgen open   --target URL --rate R [--arrivals uniform|poisson] [--seed N]
               [--max-in-flight N] [--min-sustained RATIO] --secs S --warmup W
               --requests F --results F --summary F
               [--timeout S] [--format binpb|json]
loadgen probe  --target URL --requests F --rounds N --deadline S
loadgen selftest --summary F [--format binpb|json]
loadgen --capabilities [--format binpb|json]
```

- `--target` is the base URL of every step (the gateway Service).
  `--timeout` is per step (default 10 s). `--max-in-flight` bounds the open
  loop's concurrent operations (default 512, as today); beyond it operations
  wait, which shows as send lag.
- `--arrivals` defaults to `poisson`; `uniform` spaces arrivals `1/rate`
  apart, as the servicebench engine does, and the yardsticks keep it.
- `probe` sends each planned operation `--rounds` times on fresh connections
  until each succeeds once per round or `--deadline` passes (readiness).
- Exit 0: the phase ran and the summary is written, including an exhausted
  or unsustained one (Python judges it). Exit 2: a usage error or a plan it
  cannot run (unknown call kind, unsupported schema version). Exit 1: any
  other failure, with the reason on standard error and no summary.

## Files

All in `proto/vibesys/evaluator/v1/`; see the comments there for every field.

- `loadgen.proto`: `FileHeader` (schema version, kind, writer) starts every
  file. Requests file: `RequestsHeader` (operation names, count), then
  `PlannedOperation` (operation, steps, exclusive key, keep bodies). `Step` is
  a oneof of `HttpCall` (method, path, query, headers, raw, text or form
  body, all `Template`s) and `ThriftCall` (reserved, not designed), plus
  `Capture`s and expected statuses. Results file: `OperationResult`
  (scheduled, start, end, `StepResult`s with status and the body or its
  SHA-256 and length, error kind). Summary file: `PhaseSummary` (window,
  sent, completed, errors by kind, spilled, offered and achieved rate, send
  lag histogram and maximum, exclusive-key wait, loadgen CPU fraction,
  sustained, exhausted, HDR latency overall and per operation).
  `Capabilities` and `SelftestResult`.
- `phase.proto`: `PhaseInput` (sample seed, phase index, load, history),
  `HistoryEntry`, `PhaseOutput`.

Length-delimited binary is the default; `--format json` writes proto3 JSON,
one message per line. Histograms are microseconds in the standard compressed
HdrHistogram V2 encoding, which both the Go and Python libraries read.
Conformance fixtures (`fixtures/`, work package 2) hold files of each kind
that both sides must read and write identically.

Sizes: a request of the yardsticks encodes in about 100 to 300 bytes, so a
60 s open-loop phase at 5,000 ops/s is about 300,000 operations and 60 MB of
plan, held in memory by loadgen (the phase pod has 4 GiB). Unsampled bodies
are kept as 40 bytes.

## Saturation guard and validity

The phase pod's resources are fixed by the package (6 CPUs, 4 GiB, requests
equal to limits; what both yardsticks use today), so the guard has a known
budget. A phase fails, and with it the sample, when:

- loadgen's CPU over the window exceeds 80% of its quota; or
- open loop: the 99th percentile of send lag (excluding exclusive-key waits)
  exceeds the larger of 10% of the window's p99 latency and twice the
  added latency `selftest` measured; or
- closed loop: the plan was exhausted before the window ended (the message
  says to raise `max_ops_per_sec`).

`Validity` is the task's: `max_error_rate` (loadgen errors plus failed checks,
over the window's operations), `min_sustained_ratio` (open loop: offered rate
over target rate) and `min_ops_per_operation` (every operation of the mix
completed at least this many times in the window). Both yardsticks use 0.0,
0.95 and 1, their current `[constraints]` and `min_offered_rate_ratio`.

`loadgen selftest` (work package 7) runs against an in-process server on the
phase pod's resources and reports the highest rate it sustains under the
guard and the latency it adds; the package uses it to set the guard and to
refuse a target rate above what loadgen can send.

## Seeds

- Dataset: a constant in `dataset.py`, part of the task version. Changing it
  changes the task.
- Sample: drawn fresh for every sample and recorded in the artifacts
  (`seeds.json`); `benchmark --seed N` replays one. (hotel-k8s uses fixed
  workload seeds today; social-network-k8s draws them per sample.)
- Model stream: `Rand(sample, "model")`, the same in every phase of a sample.
- Phase stream: `Rand(sample, "phase", index)`; operation `i` uses
  `child(i)`; the open loop's Poisson seed is drawn from the phase stream.

Every request of a sample is a function of the dataset, the sample seed and
the outcomes of the earlier phases' writes.

## Deploy layer (`deploy.k8s.App`)

Generic over the application, from hotel-k8s' and social-network-k8s'
run.py: the cluster comes from the vibesys-cli `kubernetes` resource
(`KUBECONFIG`, `VIBESYS_K8S_REGISTRY`, `VIBESYS_K8S_BUILDER`).

- `images`: manifest variable to candidate directory, built with buildctl
  from `PROJECT_ROOT/<dir>` and tagged by a digest of the directory.
- `manifest`: `${NAMESPACE}` and the image variables are substituted.
- `data_tier`: a label selector applied and waited for before `seed`; the
  rest of the manifest follows the seed.
- `seed(data)`: a Python function run in a Job of the task image, before any
  request reaches the application; the Job's log and summary are artifacts.
- `ready`: steps sent through `gateway` by `loadgen probe` from a pod once
  every Deployment has all its replicas available; each must succeed twice
  per ready endpoint of the gateway Service, on fresh connections.
- `restartable`, `caches`: the hooks accuracy checkers use, through
  `python bench.py ctl restart DEPLOYMENT | flush CACHE | stop | start`:
  delete one pod and wait for all replicas and the probes; `FLUSHALL` or
  `flush_all` in place; scale every restartable Deployment to 0 and back.
- Phase pods: one pod per phase in the namespace, the inputs copied in and
  the outputs copied out with `kubectl cp`, so no ConfigMap size limit
  applies; pods of the package carry its labels and die with the namespace.

## Versioning

One version `X.Y.Z` for the Python package, the Go module
(`sdk/vibesys-evaluator/loadgen/vX.Y.Z`) and the image
`vibesys-evaluator:X.Y.Z`. Python and loadgen ship together, and the Python
side still reads `loadgen --capabilities` before deploying. The schema is
`vibesys.evaluator.v1` with `schema_version` 1; `buf breaking` keeps v1
additive; a reader rejects a schema version it does not implement. The
public Python API is the generated snapshot `api/public-api.txt`, checked by
CI against the code (griffe) and against the last release (`griffe check`);
breaking it needs the user's approval ([`AGENTS.md`](../AGENTS.md)).
`Rand`'s algorithms are part of the API: changing a stream is a break.

## Out of scope

Request Factory and LLM-written requests, library and job kits, gRPC and
Thrift calls (Thrift is a reserved placeholder), a Rust loadgen (the CLI and
files are designed so one can replace the Go build), Docker Compose and
local-process deploys, CLI additions (sample seed from vibesys, diagnostics,
profile entrypoint, scope paths), the checker kit (accuracy). The deploy
layer offers the restart and flush hooks the current checkers use.

## Open questions

1. The closed loop pre-generates `max_ops_per_sec x (warmup + secs)`
   operations and fails the sample when it runs out, so a candidate faster
   than the bound fails. Keep a task-set bound (default 20,000 ops/s), or
   rerun the phase once with a larger plan (the exhausted attempt then only
   warms up; its writes stay in the history)?
2. `arrivals` defaults to `poisson`, while both yardsticks keep `uniform` to
   stay within noise. Should the default be `uniform`?
3. hotel-k8s moves from fixed workload seeds to a fresh seed per sample.
   Acceptable, or keep fixed seeds for hotel?
4. As long as the Go accuracy oracles read the Go datasets, `dataset.py` must
   reproduce them exactly (`Rand` is specified for this, and the port checks
   the seeded documents' digest against the Go seed's). Alternatively the
   oracles read a dataset file the Python side writes.
5. Generation cost: hotel's search draws a location until the geo
   reference's answer is unambiguous, so the reference runs for every search
   at generation (about 60% of the plan) in Python. If that is too slow, the
   tie test moves to check time and a tied sampled search counts as unchecked.
