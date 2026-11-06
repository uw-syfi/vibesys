# vibesys-evaluator: design spec

Status: draft. The public Python API is
[`api/public-api.txt`](../api/public-api.txt), the public Go API (the
microservices request-generator SDK) is
[`api/go-workload.txt`](../api/go-workload.txt), the microservices schema is
[`loadgen.proto`](../../../proto/vibesys/evaluator/v1/loadgen.proto) and
[`phase.proto`](../../../proto/vibesys/evaluator/v1/phase.proto), and the
example tasks written against them are in [`examples/`](../examples).

## What it is

`vibesys-evaluator` is the package a task writer builds a benchmark on. A task
(see [the vibesys-cli task environment](../../../docs/contributing/cli-task-environment.md))
is a Dockerfile, `vibesys.input.toml`, `objectives.toml`, `OBJECTIVE.md` and,
with this package, a `bench.py` whose `__main__` calls one `main`. The
Dockerfile is `FROM vibesys-evaluator-<domain>:<ver>@sha256:...` plus
`COPY . /task`. One benchmark run is one sample; it writes result-protocol-2
records ([PROTOCOL.md](../../vs-evaluator/PROTOCOL.md)) to the path after
`--vs-output`.

The package is a **domain-free core** and **domains** built on it (see
"Layout"):

- The core, `vibesys_evaluator`: the sample lifecycle, the modes `check`,
  `benchmark` and `profile`, result-protocol-2 records, `Metric`,
  `Validity`, `Rand`, `Run` (named phase directories and `report`) and
  teardown on every exit path. It knows no system under test.
- `vibesys_evaluator.microservices`: microservices over HTTP on Kubernetes,
  with **loadgen**, a Go binary that sends the requests. A task's `bench.py`
  holds (1) the system under test (build, deploy, readiness), (2) the
  initial data (seed, from `dataset.py`, a deterministic generator), (3) the
  workload (operation functions and the model they are checked against),
  (4) a `benchmark(run)` function that is the scoring definition, and (5)
  the validity rules. The yardsticks are the `hotel-k8s` and
  `social-network-k8s` tasks of the DeathStarBench fork, rewritten on it
  with no code shared between them, each measuring within noise of its
  current `run.py`.
- `vibesys_evaluator.llm_serving`: an OpenAI-compatible LLM server that the
  agent's candidate image runs and the task container reaches only over
  HTTP (see "LLM serving").

Each domain ships as its own image (see "Versioning"); everything has one
version.

## Decisions this spec builds on

Recorded with the user before this spec; not reopened here.

- The core is domain-free; each system kind is a domain package on top of
  it. Domains import the core only, never each other, and a mechanism of
  the core (the sample, the records, the modes, the harness runner) names no
  domain. Each domain is published as its own image.

The microservices domain:

- No recipe concept: `benchmark(run)` composed from phase primitives
  (`run.closed_loop`, `run.open_loop`) is the scoring definition.
- Operations are functions `op(model, rand) -> Request` in a weighted mix.
  Responses are validated by per-operation functions, after the phase, over a
  sample (writes 100%, reads 10% by default).
- `python /task/bench.py` in the task container only orchestrates (deploy,
  phases, records, teardown). Each phase runs as an in-cluster pod of the task
  image. In it, loadgen starts the task's request generator and reads the
  requests from its standard output while it runs the phase (a stream, never
  a file: a closed loop cannot run out), writes results (from a writer thread
  that never blocks senders) and a summary; the generator checks the
  responses after. The generator runs on the phase pod's CPUs, never the
  application's, and loadgen measures whether it kept up.
- The request stream is deterministic: request `i` depends only on the sample
  seed, the phase and the requests before it (the model as the planned writes
  left it), never on timing; timing decides only how many are consumed.
- Front ends: the Python SDK (`vibesys_evaluator.microservices`) and the Go SDK
  (`loadgen/workload`) are two languages for one contract (the generator
  command line and the stream); the same workload writes the same stream in
  both, and checks run in the front end's language.
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
- One version for the Python package, the Go module tag and the images.
- Public API rule: see [`AGENTS.md`](../AGENTS.md).

## Architecture

```text
 vibesys-cli ── docker run ──▶ task container (task image)
                               python /task/bench.py benchmark --vs-output F
                               ┌────────────────────────────────────────────┐
                               │ orchestrator (microservices.main)           │
                               │  hello record ─▶ F                          │
                               │  k8s.App: build images (buildctl), fresh    │
                               │  namespace, data tier, seed Job, services,  │
                               │  readiness                                  │
                               │  benchmark(run): for each phase ─┐          │
                               │  result/error record ─▶ F        │          │
                               │  teardown (every exit path)      │          │
                               └──────────────────────────────────┼──────────┘
              kubectl cp PhaseInput (seeds, load, history) ──────▼
 ┌──────────────── phase pod (task image, in the namespace) ─────────────────┐
 │ python bench.py phase: writes the dataset file, then runs                 │
 │  1 loadgen open|closed ... -- GENERATOR generate --phase-input F ...      │
 │      GENERATOR (python bench.py, or the task's Go binary):                │
 │        dataset ─▶ model ─▶ replay history (apply); then for i = 0, 1, ... │
 │        pick op by weight, op.build(model, rand) ─▶ stdout (the stream)    │
 │        (writes applied as pending; sampled ops and writes kept in --state)│
 │      loadgen: buffer filled before t0, refilled while it sends            │
 │        ──▶ gateway Service ──▶ application pods; results + summary files  │
 │      window over: loadgen closes the stream, the generator exits          │
 │  2 GENERATOR check: rebuild model, replay history + this phase's writes,  │
 │      check each sampled call in start order ─▶ PhaseOutput                │
 │  3 saturation and validity guards; PhaseOutput ─▶ kubectl cp back         │
 └───────────────────────────────────────────────────────────────────────────┘
```

The diagram is the microservices domain. Dependencies point one way:
`bench.py` imports `vibesys_evaluator.microservices`; a Go generator imports
`loadgen/workload`; the phase pod runs loadgen and the generator as
subprocesses that speak the `.proto`; loadgen knows nothing of operations,
models or tasks.

## Layout

```text
 bench.py ──▶ vibesys_evaluator.microservices ──┐
          └─▶ vibesys_evaluator.llm_serving ────┼──▶ vibesys_evaluator (core)
          └─▶ vibesys_evaluator (core) ─────────┘
 a Go generator ──▶ loadgen/workload (the microservices domain's Go SDK)
```

The core owns one concern per seam, each a private module behind the public
names:

| Concern | Seam |
| --- | --- |
| The sample | `Sample`: the mode, the seed, the artifacts directory, the records, a partial result for an error record, named phase directories |
| Modes and the command line | `main(metrics=, benchmark=, check=, profile=)`: `check`, `benchmark --vs-output F [--seed N]`, `profile [--seed N]`; a mode the task does not define exits 2 |
| Records | result protocol 2: `hello`, then one `result` or `error`; an error carries the partial result |
| Expected failures | `SampleError(message, partial=)`: a typed failure that ends the sample with an `error` record, not a traceback |
| Signals | SIGTERM, SIGINT and SIGHUP stop the sample through the same teardown as a return; the exit status is 128 plus the signal |
| A harness subprocess | `Runner(argv, timeout_secs=, cwd=)`: runs in its own process group, killed with the group on every exit path |

A domain composes these into its own `main` and may subclass `Run` with its
phase primitives (`microservices.Run` adds `closed_loop` and `open_loop`).
The core's `main` is enough for a domain whose phases are plain functions
(`llm_serving`). A domain's `main` has its own command line: the
microservices script's commands are `benchmark`, `phase`, `generate`,
`check` (the generator's, see "Generator command line"), `seed` and `ctl`.

The rule "the core imports no domain; a domain imports the core and itself
only" is checked by `python/tests/test_layering.py`, which reads every
module's imports (the repository's tach graph does not cover `sdk/`). The Go
SDK is microservices code in Go; it imports only loadgen's generated schema.

## The runtime, phase by phase

### The sample (orchestrator)

1. Parse the command line: `benchmark --project-root DIR --vs-output FILE`
   (and `ctl ...`, see "Deploy layer"). Draw the sample seed; write and flush
   `hello` with the declared metrics.
2. Deploy (`microservices.k8s.App`): sweep this task's stale namespaces; build the
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
2. **Generate.** The generator draws operations from the mix by weight with
   the phase stream, one after another for as long as loadgen reads; operation
   `i` gets its own child stream. `build(model, rand)` returns a `Request`.
   For a write (an operation with `apply`), `apply` runs at once with a
   pending `Call` (no times, `ok` None), so later operations of the same
   stream build on it (a follow, then an unfollow of that edge). Whether the
   operation is in the validation sample is drawn here too, so the sample does
   not depend on timing. There is no plan size: the stream has no end.
3. **Run.** `loadgen` starts the generator, fills its buffer from the stream
   (the generator's start-up: dataset, model, history), then starts t0.
   Clients take operations in stream order; at the window's end no new
   operation starts, loadgen closes the stream (the generator exits), and
   those in flight complete, bounded by the request timeout. Operations
   generated but never taken are never sent and do not enter the history.
4. **Check.** The generator's `check` rebuilds the model from the history
   plus this phase's sent writes, then calls `check(model, call)` for every
   sampled operation in order of start time. The results file is streamed
   and only the results of writes and sampled operations are kept, so the
   check's memory grows with the sample, not the phase (a model may learn
   from earlier responses, as social network's first sight of a live post
   does). Writes
   are always in the sample. A check returns `None` (Go: nil) or what is
   wrong.
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

Listing: [`api/public-api.txt`](../api/public-api.txt).

| Module | Names |
| --- | --- |
| `vibesys_evaluator` | `main`, `Run`, `Metric`, `Validity`, `Rand` |
| `vibesys_evaluator.microservices` | `main`, `Run`, `PhaseResult`, `Versions`, `Call`, `Response`, `Workload`, `Generator`, `Operation`, `Request`, `Step`, `Template`, `get`, `post`, `put`, `ref` |
| `vibesys_evaluator.microservices.k8s` | `App` |
| `vibesys_evaluator.llm_serving` | `endpoint` |
| `vibesys_evaluator.llm_serving.agentx` | `replay`, `METRICS`, `HARNESS_VERSION` |
| `vibesys_evaluator.llm_serving.golden` | `logprobs`, `Thresholds` |

The microservices names:

- `Operation(name, weight, build, check, apply, sample)`. `apply` makes an
  operation a write; `sample` defaults to 1.0 for writes and 0.1 otherwise,
  and a write may not set it lower.
- `Request(*steps, info, exclusive)`. Steps are sent in order on one client
  and form one operation with one latency. `info` (JSON) holds what the
  request was built from; it is not sent and comes back in the `Call`.
  Operations with the same `exclusive` key never overlap; loadgen grants the
  key in stream order, so writes to one item are applied in stream order.
- `get`/`post`/`put(path, *, query, form, json, body, headers, capture, expect)`.
  `capture={"token": "/token"}` takes a value from the step's response by
  JSON pointer; `ref("token")` puts it into a later step (`"Bearer " +
  ref("token")` is a `Template`). `expect` is the status (or statuses) that
  count as success for loadgen's error count; the default is 200.
- `Rand(seed, *labels)`: SplitMix64 streams with FNV-1a label mixing,
  `child`, `random`, `below`, `weighted`, `normal` (Irwin-Hall of four),
  `zipf(n, s)` (inverse CDF, cached per `(n, s)`; an exponent other than 1,
  1/2 and 3/4 weighs rank `k` by `exp(-s log k)` with fdlibm's algorithms in
  plain IEEE operations, never the platform's `pow`). The algorithms are part
  of the versioned contract, equal the Go generators' `newRNG`/`Mix` and the
  Go SDK's `workload.Rand` bit for bit on every platform
  (`fixtures/rand.json`), so a dataset is reproducible across versions and a
  port from Go can be exact.
- `Versions(bound_secs)`: `write(key, value, call)`, `latest(key, default)`
  (generation: pending writes included), `allowed(key, default, start=, end=)`
  (checks).
- `Run.closed_loop(clients, secs, warmup)`,
  `Run.open_loop(rate, secs, warmup, arrivals)`, and the core's
  `Run.report(values)` and `Run.phase(name)`. `PhaseResult` has
  `ops_per_sec`, `p50_ms`, `p99_ms`.
- `Generator(command)`: a workload whose requests and checks are another
  program (a Go generator built on `loadgen/workload`, in the task image),
  run as `command + ["generate", ...]` and `command + ["check", ...]`
  ("Generator command line"); `microservices.main(workload=Generator([...]))`.
- `main(dataset, system, workload, benchmark, metrics, validity)`.

## The Go SDK

`github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/workload`, a package
of loadgen's module, so it has the one version and tag
(`sdk/vibesys-evaluator/loadgen/vX.Y.Z`) and shares the generated schema
types; it imports only the protobuf runtime (no cgo), so a task's own Go
module (with its reference model) imports it cheaply. Listing:
[`api/go-workload.txt`](../api/go-workload.txt). The concepts are the Python
service API's:

- `Workload[D, M]{Dataset, Model, Operations}`: `Dataset(path)` loads the
  dataset file the orchestrator writes (the JSON of `bench.py`'s `dataset()`),
  `Model(data, rand)` builds the model; D and M are any Go types (a task's
  existing reference state).
- `Operation[M]{Name, Weight, Build, Check, Apply, Sample}`: `Build(m, rand)
  Request`, `Check(m, call) error` (nil when right), `Apply(m, call) error`
  makes it a write; `Sample` 0 is the default (1 for writes, 0.1 otherwise).
- `Request{Steps, Info, Exclusive}`; `Get`/`Post`/`Put(path, options...)` with
  `Query`, `Header`, `Form`, `JSON`, `Body`, `Capture`, `Expect`; `Ref` and
  `Concat` build a `Template`. `JSON` encodes as Python's `json.dumps` does
  (compact, non-ASCII escaped; `Object` keeps key order, a map sorts its keys,
  a struct follows its fields), so a Go and a Python workload send the same
  bytes.
- `Call`, `Response`, `Versions[K, V]` (the visibility rule), `Rand`
  (bit-identical to Python's; `Uint64` is Python's `below(2**64)`).
- `Main(workload)` is the generator command.

## Generator command line

The phase pod runs every generator, Python or Go, alike:

```text
GENERATOR generate --phase-input F --dataset F --state DIR
GENERATOR check    --phase-input F --dataset F --state DIR
                   --results F --summary F --output F
```

- `GENERATOR` is `python bench.py` for a Python workload (its `generate` and
  `check` commands) or the `microservices.Generator` command.
- `generate` writes the request stream (`FileHeader` kind requests,
  `RequestsHeader`, then `PlannedOperation`s, binpb) to standard output until
  the stream is closed, then exits 0. It keeps a `KeptOperation` for every
  sampled operation and write in `--state`, written before the operation, so
  every operation loadgen read has its record. Standard error carries only
  logs. It never ends the stream itself.
- `check` reads the `PhaseInput`, its `--state`, loadgen's results and summary
  files, and writes the `PhaseOutput` (summary, checks, violations, this
  phase's writes for the history).
- Exit 0 on success, 2 for a usage error, 1 for any other failure (a `build`,
  `apply` or dataset error, naming the operation).

Where the examples show the hard parts:

- Multi-step template: `examples/kv-store/bench.py` (`login`: log in, capture
  the token, send it).
- The larger tasks, with cross-phase state (the history replaces the write
  journal, `--state-out`/`--state-in` and run.py's ConfigMaps) and writes
  checked within the 1 s bound, are the `hotel-k8s` and `social-network-k8s`
  tasks under `.vibesys/tasks/` in the DeathStarBench fork
  (`vibesys-playground/DeathStarBench`, `vibesys` branch), which the
  repository's `examples/microservices/repositories/deathstarbench` submodule
  checks out.

## loadgen command line

```text
loadgen closed --target URL --clients N --secs S --warmup W
               --results F --summary F [--timeout S] [--buffer N]
               [--format binpb|json] -- GENERATOR [ARG...]
loadgen open   --target URL --rate R [--arrivals uniform|poisson] [--seed N]
               [--max-in-flight N] [--min-sustained RATIO] --secs S --warmup W
               --results F --summary F [--timeout S] [--buffer N]
               [--format binpb|json] -- GENERATOR [ARG...]
loadgen probe  --target URL --requests F --rounds N --deadline S
               [--timeout S] [--format binpb|json]
loadgen selftest --summary F [--step-secs S] [--max-rate R] [--format binpb|json]
loadgen --capabilities [--format binpb|json]
```

- The generator command after `--` is started as a child process with
  loadgen's environment; loadgen reads the request stream from its standard
  output and passes its standard error through. `--buffer` (default 8,192)
  is how many decoded operations loadgen holds ahead of the senders: it fills
  them before t0 and keeps them filled, so the bounded buffer and the pipe are
  the backpressure on the generator. At the end loadgen closes the stream and
  waits 10 s for the generator to exit, then kills it. A stream that ends, or
  a generator that exits, while loadgen still reads fails the phase (exit 1,
  with the generator's exit status).
- `--target` is the base URL of every step (the gateway Service).
  `--timeout` is per step (default 10 s). `--max-in-flight` bounds the open
  loop's concurrent operations (default 512, as today); beyond it operations
  wait, which shows as send lag.
- `--arrivals` defaults to `poisson`; `uniform` spaces arrivals `1/rate`
  apart, as the servicebench engine does, and the yardsticks keep it.
  `--min-sustained` defaults to 0.95 (the default `min_sustained_ratio`).
- Latency is end minus scheduled in both loops; a closed-loop operation is
  scheduled when its client takes it, so it differs from end minus start only
  by the exclusive-key wait. The latency histograms hold successful
  operations; failed ones are counted in `errors`.
- At the window's end no operation starts; those in flight get until the
  window's end plus `--timeout`, then fail CANCELLED. An open-loop operation
  due in the window but not started by then is recorded CANCELLED with start
  0 (never sent).
- `selftest` tries uniform open loops of `--step-secs` (default 2) from
  1,000 operations/s, doubling up to `--max-rate` (default 200,000) and
  bisecting, against an in-process server.
- `probe` sends each operation of a finite request stream (the task's ready
  steps, `--requests`) `--rounds` times on fresh connections until each
  succeeds once per round or `--deadline` passes (readiness). It is the only
  command that reads requests from a file; a phase never does.
- Exit 0: the phase ran and the summary is written, including an unsustained
  one or one whose generator fell behind (Python judges it). Exit 2: a usage
  error or a stream it cannot run (unknown call kind, unsupported schema
  version, an invalid operation). Exit 1: any other failure, with the reason
  on standard error and no summary.

## Files

All in `proto/vibesys/evaluator/v1/`; see the comments there for every field.

- `loadgen.proto`: `FileHeader` (schema version, kind, writer) starts every
  stream and file. Request stream: `RequestsHeader` (operation names), then
  `PlannedOperation`s (operation, steps, exclusive key, keep bodies) with no
  end. `Step` is
  a oneof of `HttpCall` (method, path, query, headers, raw, text or form
  body, all `Template`s) and `ThriftCall` (reserved, not designed), plus
  `Capture`s and expected statuses. Results file: `OperationResult`
  (scheduled, start, end, `StepResult`s with status and the body or its
  SHA-256 and length, error kind). Summary file: `PhaseSummary` (window,
  sent, completed, errors by kind, spilled, offered and achieved rate, send
  lag histogram and maximum, exclusive-key wait, loadgen CPU fraction,
  sustained, HDR latency overall and per operation, results spilled, the
  operation names, and `GeneratorStats`: operations read, the window's
  operations that waited for the generator and the wait histogram, the rate
  the generator sustained while the buffer had room, its start-up time). Selftest file: `SelftestResult`; `--capabilities` prints a
  `Capabilities`; both after their own `FileHeader` kind.
- `phase.proto`: `PhaseInput` (sample seed, phase index, load, history),
  `HistoryEntry`, `PhaseOutput`, and `KeptOperation` (a generator's own
  record, in `--state`, of what it must check).

Length-delimited binary is the default; `--format json` writes proto3 JSON,
one message per line. Histograms are microseconds in the standard compressed
HdrHistogram V2 encoding, which both the Go and Python libraries read (the
raw bytes, not their base64 text). Conformance fixtures
([`fixtures/`](../fixtures), indexed by `fixtures/manifest.json`) hold files
of each kind that both sides must read and write identically, and files a
reader must reject.

Sizes: a request of the yardsticks encodes in about 100 to 300 bytes; loadgen
holds only its buffer (8,192 operations, a few MB), whatever the phase's
length or rate. Unsampled bodies are kept as 40 bytes.
A phase pod killed at its memory limit (OOMKilled) fails the sample with an
error that says so and names the limit; what to lower is the generator's
model or the sample (`Operation.sample`), since loadgen's own memory is
bounded by its buffer.

## Saturation guard and validity

The phase pod's resources are fixed by the package (6 CPUs, 4 GiB, requests
equal to limits; what both yardsticks use today), so the guard has a known
budget. A phase fails, and with it the sample, when:

- loadgen's CPU over the window exceeds 80% of its quota; or
- open loop: the 99th percentile of send lag (excluding exclusive-key waits)
  exceeds the larger of 10% of the window's p99 latency and twice the
  added latency `selftest` measured, unless at least 1% of the window's
  operations were due while every sender was busy (`saturated`): then the
  application held the senders with slow responses, and the lag is its
  queue, already in its latency (end minus scheduled). Lag while senders
  are free still fails the phase; or
- the request generator fell behind: more than 1% of the window's operations
  waited for it (`GeneratorStats.waited`); the message names the rate the
  generator sustained (`GeneratorStats.rate`), so the writer knows to make it
  faster (or write it in Go) rather than read a slow generator as a slow
  application. A wait for the generator is never counted as saturation, so
  the two rules cannot excuse each other.

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

## Deploy layer (`microservices.k8s.App`)

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

## LLM serving (`llm_serving`)

The candidate image runs an OpenAI-compatible server; vibesys-cli starts it,
waits for its ready path and sets `VIBESYS_SERVICE_URL` in the task
container to its base URL (scheme, host and port, no path) for the
`[accuracy]`, `[benchmark]` and `[profile]` commands of a `version = "0.2"`
task. The task container has no GPU; nothing the candidate installs reaches
it.

- `endpoint()` reads `VIBESYS_SERVICE_URL`, strips a trailing slash and
  refuses an unset, empty or non-`http(s)://host[:port]` value with a
  message naming the variable and who sets it.
- `agentx.replay(model=, tokenizer=, tokenizer_revision=, concurrency=,
  max_context_length=, artifacts=, url=None, mode="full")` is a light
  wrapper over `agentx-harness` (AIPerf) at `HARNESS_VERSION` (1.0.7), which
  the llm-serving image pins: it checks the installed harness is that
  version before anything runs, builds the `agentx submit` command (scenario
  `agentx`, the date-pinned corpus, seed 42, trajectory start ratios
  0.25 to 0.75; `full`: 900 s after ten warmup requests per lane; `smoke`:
  60 s, one warmup request, two corpus entries), runs it through the core's
  harness runner in an empty `artifacts` directory, and translates the
  export into the six `METRICS` (output tokens/s, TTFT and TPOT p50/p90, p90
  request latency). It fails closed: a missing or invalid submission, no
  measured request, any error (the error summary must be present and empty),
  an unexpected unit, or a non-finite or negative value is an
  `error` record with the partial values, never a score.
- `golden.logprobs(path, url=None, model=None, thresholds=None, report=None)`
  is the accuracy gate, ported from the Qwen task's HTTP checker: free-run
  divergences only at near-ties of the reference, no decisive teacher-forced
  argmax flips, bounded mean and p99 |Δlogprob|, a minimum matched-prefix
  fraction, the same over chained session rounds (the prefix-cache resume
  path), and a streamed response equal to the non-streamed one with a
  correct usage chunk. It returns `None` on a pass or what failed; `Thresholds`
  holds the bounds.

A task's `bench.py` passes its `check`, `benchmark` and `profile` functions
to the core's `main`; `profile` runs a smoke replay, the load a profiler
records, and is never scored. Example:
`examples/model-serving/qwen3.5-9b-mi210/.vibesys/tasks/agentx/` in the
repository, and [`examples/agentx-llm/bench.py`](../examples/agentx-llm/bench.py).

## Versioning

One version `X.Y.Z` for the Python package, the Go module
(`sdk/vibesys-evaluator/loadgen/vX.Y.Z`) and the images, one per domain:
`vibesys-evaluator-microservices:X.Y.Z` (the package and loadgen) and
`vibesys-evaluator-llm-serving:X.Y.Z` (the package and `agentx-harness`,
installed from a hash-pinned lock in `image/llm-serving/`; task writers never
install the harness). Python and loadgen ship together, and the Python
side still reads `loadgen --capabilities` before deploying. The schema is
`vibesys.evaluator.v1` with `schema_version` 1; `buf breaking` keeps v1
additive; a reader rejects a schema version it does not implement. The
public Python API is the generated snapshot `api/public-api.txt`, checked by
CI against the code (griffe) and against the last release (`griffe check`);
breaking it needs the user's approval ([`AGENTS.md`](../AGENTS.md)), and
an approved Python break is recorded in `api/approved-breaks.toml` against
the release it breaks, which the check then accepts for that release only.
The public Go API is `api/go-workload.txt` (`go doc -all` of
`loadgen/workload`), checked against the code (a Go test) and against the last
release (no declaration of it may change or go away).
`Rand`'s algorithms are part of the API: changing a stream is a break.

## Out of scope

Request Factory and LLM-written requests, library and job kits, gRPC and
Thrift calls (Thrift is a reserved placeholder), a Rust loadgen (the CLI and
files are designed so one can replace the Go build), Docker Compose and
local-process deploys, CLI additions (sample seed from vibesys, diagnostics,
scope paths), a checker kit for microservices (the deploy layer offers the
restart and flush hooks the current checkers use), and LLM serving
workloads other than AgentX replay and the golden-logprob gate.

## Open questions

1. `arrivals` defaults to `poisson`, while both yardsticks keep `uniform` to
   stay within noise. Should the default be `uniform`?
2. hotel-k8s moves from fixed workload seeds to a fresh seed per sample.
   Acceptable, or keep fixed seeds for hotel?
3. As long as the Go accuracy oracles read the Go datasets, `dataset.py` must
   reproduce them exactly (`Rand` is specified for this, and the port checks
   the seeded documents' digest against the Go seed's). Alternatively the
   oracles read a dataset file the Python side writes.
4. Generation cost: hotel's search draws a location until the geo
   reference's answer is unambiguous, so the reference runs for every search
   at generation (about 60% of the stream), about 110 us an operation in
   Python. The generator guard reports it if that cannot keep up; the
   answer is hotel's generator in Go on its existing reference model.
