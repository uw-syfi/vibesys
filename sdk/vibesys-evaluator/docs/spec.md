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
  (`go/workload`) are two languages for one contract (the generator
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
              kubectl cp PhaseInput (seeds, load, capture) ──────▼
 ┌──────────────── phase pod (task image, in the namespace) ─────────────────┐
 │ python bench.py phase: writes the dataset file, then the core's           │
 │ `vibesys-evaluator phase pod` (a Go task binary: its own `phase`) runs    │
 │  loadgen open|closed ... -- GENERATOR generate --phase-input F ...        │
 │      GENERATOR (python bench.py, or the task's Go binary):                │
 │        dataset ─▶ model ─▶ begin_phase; then for i = 0, 1, ...            │
 │        pick op by weight, op.build(model, rand) ─▶ stdout (the stream)    │
 │        (under capture: every op kept, with its info, in --state)          │
 │      loadgen: buffer filled before t0, refilled while it sends            │
 │        ──▶ gateway Service ──▶ application pods; results + summary files  │
 │      window over: loadgen closes the stream, the generator exits          │
 │  PhaseOutput (summary) and the files ─▶ kubectl cp back                   │
 └───────────────────────────────────────────────────────────────────────────┘
   orchestrator: `phase judge` (saturation guard, validity) ─▶ PhaseResult
```

The diagram is the microservices domain. Dependencies point one way:
`bench.py` imports `vibesys_evaluator.microservices`; a Go generator imports
`go/workload`; the phase pod runs loadgen and the generator as
subprocesses that speak the `.proto`; loadgen knows nothing of operations,
models or tasks.

## Layout

```text
 bench.py ──▶ vibesys_evaluator.microservices ──┐
          └─▶ vibesys_evaluator.llm_serving ────┼──▶ vibesys_evaluator (core)
          └─▶ vibesys_evaluator (core) ─────────┘
 a Go generator ──▶ go/workload (the microservices domain's Go SDK)
 a Go benchmark ──▶ go/microservices (the phase runner) ──▶ go/evaluator
 a Go oracle ─────▶ go/check (the accuracy checker)
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
phase primitives (`microservices.Run` adds `closed_loop`, `open_loop` and
`fault`).
The core's `main` is enough for a domain whose phases are plain functions
(`llm_serving`). A domain's `main` has its own command line: the
microservices script's commands are `benchmark`, `accuracy`, `phase`,
`generate` and `accuracy-workload generate` (the generators', see "Generator
command line"), `seed` and `ctl`.

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

The orchestrator writes the `PhaseInput` (sample seed, phase index, load,
capture) into the phase pod; the pod runs the core's `phase pod`
(`vibesys-evaluator phase pod`, or the Go `microservices.Main`'s `phase`),
which runs loadgen with the generator (the loadgen the Python SDK names in
the request, else the one next to the running binary, else `loadgen` on
`PATH`); the orchestrator copies the pod's files back and judges them
(`phase judge`, or the Go phase runner in process). The phase runner is Go (`go/microservices`, `go/internal/phase`);
the Python `Run` drives it through those two verbs.

1. **Model.** The generator builds `workload.model(data, rand)` with the
   sample's model stream (the same in every phase), then
   `begin_phase(model, index)` when the workload has one. Every phase starts
   from a model built afresh: nothing an earlier phase sent is replayed into
   it, and nothing is reset in the application between phases, so a later
   phase works on what the earlier ones wrote.
2. **Generate.** The generator draws operations from the mix by weight with
   the phase stream, one after another for as long as loadgen reads; operation
   `i` gets its own child stream. `build(model, rand)` returns a `Request`,
   and may change the model so that later operations build on it (an update,
   then a read of the new value). There is no plan size: the stream has no
   end.
3. **Run.** `loadgen` starts the generator, fills its buffer from the stream
   (the generator's start-up: dataset and model), then starts t0. Clients take
   operations in stream order; at the window's end no new operation starts,
   loadgen closes the stream (the generator exits), and those in flight
   complete, bounded by the request timeout. Operations generated but never
   taken are never sent.
4. **Files.** The pod leaves loadgen's summary and results (compressed) and a
   `PhaseOutput` (the summary and loadgen's run time) in its work directory.
5. **Judge.** The orchestrator applies the saturation guard and the validity
   rules to the summary: the phase is a measurement, or the sample ends with
   an `error` record naming the phase and the reason.

A benchmark only measures. Whether the responses are right is an accuracy
check's to judge (`go/check`), from the history a run with capture records:
under `capture` every operation keeps its response bodies and its loadgen
client, the generator keeps every operation's info (`KeptOperation`), the
pod leaves loadgen's results whole, and the orchestrator writes
`history.binpb` with the phases and the faults run between them
(`Run.fault`; see "Accuracy history").

### Time windows

loadgen's clock in the pod is the only clock of a phase. With `W` the
warmup and `S` the window: the warmup is `[t0, t0+W)`, the measurement window
`[t0+W, t0+W+S)`. Latency, throughput, error rate and the validity rules use
the operations scheduled (open loop) or started (closed loop) in the window.
Throughput is the operations completed in the window divided by `S`;
operations started in the window that end after it are counted as `spilled`.

A fault (`Run.fault`) runs between two phases, and its start and end are
taken on the orchestrator's clock, which must agree with the phase pods'
(loadgen stamps its results with theirs): a cluster's synchronized clocks,
or one machine. The phase runner logs a fault that starts before the last
phase's window ended on loadgen's clock: the clocks disagree by at least as
much.

## The Python API

Listing: [`api/public-api.txt`](../api/public-api.txt).

| Module | Names |
| --- | --- |
| `vibesys_evaluator` | `main`, `Run`, `Metric`, `Validity`, `Rand` |
| `vibesys_evaluator.microservices` | `main`, `Run`, `PhaseResult`, `Workload`, `Generator`, `Operation`, `Request`, `Step`, `Template`, `http`, `get`, `post`, `put`, `ref`, `Accuracy`, `Fault` |
| `vibesys_evaluator.microservices.k8s` | `App`, `attach`, `Deployment`, `ExecResult`, `ReplacedPod` |
| `vibesys_evaluator.llm_serving` | `endpoint` |
| `vibesys_evaluator.llm_serving.agentx` | `replay`, `METRICS`, `HARNESS_VERSION` |
| `vibesys_evaluator.llm_serving.golden` | `logprobs`, `Thresholds` |

The microservices names:

- `Workload(model, operations, begin_phase)`. `begin_phase(model, index)`,
  when given, is called once per phase before the first `build`.
- `Operation(name, weight, build)`. `build(model, rand)` may change the
  model; later operations of the stream see the change. A name is what
  loadgen takes (`^[a-z][a-z0-9_]{0,62}$`: no dots or dashes), unique in the
  workload; a bad one is refused when the operation is made, and loadgen
  refuses a generator's at the stream's header, before any load.
- `Request(*steps, info)`. Steps are sent in order on one client
  and form one operation with one latency. `info` (JSON) holds what the
  request was built from; it is not sent, and under capture it is kept for
  the accuracy check (`KeptOperation.info_json`).
- `http(method, path, *, query, form, json, body, headers, capture, expect)`
  for `GET`, `POST`, `PUT`, `DELETE` and `PATCH` (the methods `loadgen.proto`
  has), and the shorthands `get(path, *, query, headers, capture, expect)`,
  `post` and `put` (the arguments of `http`).
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
- `Run.closed_loop(clients, secs, warmup)`,
  `Run.open_loop(rate, secs, warmup, arrivals)`, `Run.fault(kind, target,
  action)` (between phases; recorded under capture), and the core's
  `Run.report(values)` and `Run.phase(name)`. `PhaseResult` has
  `ops_per_sec`, `p50_ms`, `p99_ms`.
- `Generator(command)`: a workload whose requests are another program (a Go
  generator built on `go/workload`, in the task image), run as
  `command + ["generate", ...]` ("Generator command line");
  `microservices.main(workload=Generator([...]))`.
- `main(dataset, system, workload, benchmark, metrics, validity, capture,
  accuracy)`. `capture=True` records the accuracy history; benchmarks leave
  it off. `accuracy` is the task's accuracy check, `bench.py accuracy`
  ("Accuracy run").
- `Accuracy(phases, rate, secs, oracle, warmup, arrivals, faults)`: the
  check's open-loop phases, the oracle's command (a program on
  `check.Main`; a relative path is the task script's directory's), and the
  faults between phases: `Fault.restart(deployment, after=i)` (one pod),
  `Fault.restart_all(deployment, after=i)` (every pod at once) and
  `Fault.exec(deployment, argv, after=i)` (a command in the first pod), each
  on a Deployment of the `k8s.App` after phase `i`.

## The Go SDK

`github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/workload`, a package
of loadgen's module, so it has the one version and tag
(`sdk/vibesys-evaluator/go/vX.Y.Z`) and shares the generated schema
types; it imports only the protobuf runtime (no cgo), so a task's own Go
module (with its reference model) imports it cheaply. Listing:
[`api/go-workload.txt`](../api/go-workload.txt). The concepts are the Python
service API's:

- `Workload[D, M]{Dataset, Model, BeginPhase, Operations}`: `Dataset(path)`
  loads the dataset file the orchestrator writes (the JSON of `bench.py`'s
  `dataset()`), `Model(data, rand)` builds the model, `BeginPhase(model,
  index)` tells it the phase; D and M are any Go types (a task's existing
  reference state).
- `Operation[M]{Name, Weight, Build}`: `Build(m, rand) Request`, which may
  change the model. Names follow the Python rule; `microservices.Main`
  refuses a bad one before it deploys, and the generator before it writes
  its stream.
- `Request{Steps, Info}`; `HTTP(method, path, options...)` for GET, POST,
  PUT, DELETE and PATCH, and its shorthands `Get`/`Post`/`Put(path,
  options...)`, with `Query`, `Header`, `Form`, `JSON`, `Body`, `Capture`,
  `Expect`; `Ref` and `Concat` build a `Template`. `JSON` encodes as Python's
  `json.dumps` does (compact, non-ASCII escaped; `Object` keeps key order, a
  map sorts its keys, a struct follows its fields), so a Go and a Python
  workload send the same bytes.
- `Rand` (bit-identical to Python's; `Uint64` is Python's `below(2**64)`).
- `Main(workload)` is the generator command.

`go/microservices` is the phase runner (listing:
[`api/go-microservices.txt`](../api/go-microservices.txt)), and a whole
benchmark in Go: `microservices.Main(microservices.Task[D, M]{Metrics,
Validity, Dataset, System, Seed, Workload or Generator, Benchmark,
Capture})` has the Python main's command line (`benchmark`, `phase`,
`generate`, `seed`); `Run` adds `ClosedLoop(ctx, Closed{Clients, Secs,
Warmup})`, `OpenLoop(ctx, Open{Rate, Secs, Warmup, Arrivals})` and
`Fault(ctx, kind, target, action)` to the core's `evaluator.Run`;
`Local(target)` is a `System` already running here (seed and phases as
child processes), `K8s(app, ready...)` an application on Kubernetes (package
`k8s`). `Task.Accuracy` is the task's accuracy check, the `accuracy` command
("Accuracy run"): `Accuracy[D, M]{Phases, Load, Faults, Oracle, Options,
Workload or Generator}` (the check's own workload; neither: the Task's) with the
faults `Restart(after, deployment)`, `RestartAll(after, deployment)`,
`Exec(after, deployment, argv...)`, `Flush(after, cache)` (on `K8s`) and
`FaultFunc(after, kind, target, action)` (the task's own), and the oracle's
specs run in-process.

## Accuracy checks (`go/check`)

`github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check` decides whether
a captured history of a candidate's responses is one the task's oracle
allows. Listing: [`api/go-check.txt`](../api/go-check.txt); the authoring
guide is [`accuracy-checks.md`](accuracy-checks.md). The oracle is Go, one
or more `Spec[S, I, O]`s, all of which must hold:

- `Project(phases, out)` turns the history's `Call`s, phase by phase, into
  `Piece`s (keys, input, output, `Unknown`, `Write`, `Origin`) and
  `Violation`s (a response wrong on its own). It may range `phases` more
  than once, each pass reading the history again: facts about the whole
  history from a first pass, pieces from a second, in the memory of one
  phase. A piece without keys touches no state: it is counted (toward
  `MaxLostShare` when unknown) and never searched;
- `Init(keys)`, `Step(state, op) (states, reason)`, optionally
  `Fault(state, keys, fault)`, `Equal`/`Hash`, and `EffectWindow` (an
  unknown outcome takes effect within it, or never).

`Check(history, specs, options)` projects each spec (stopping after a phase
with violations), fails when more than `MaxLostShare` (default 1%) of the
calls have an unknown outcome (a call counts once, however many pieces it
became; this is decided before any search), splits the pieces into partitions (the
connected components of their keys), keeps every write and at most
`MaxReadsPerKey` reads per key (a seeded sample), and searches each
partition with porcupine for an order that respects real time and that the
model accepts: largest first, in parallel, first over prefixes of doubling
length, then whole. Budgets: time per partition and in total, model steps,
a heap budget whose watchdog aborts the search with the most steps (retried
alone once the others are done), and a cap on ops per partition. The
verdict is FAIL (a violation, an illegal partition, too many lost
responses; the first violation explained with porcupine's HTML timeline),
else UNKNOWN (an undecided partition, a task or capture problem; never a
pass), else PASS, with coverage and the most ops of one partition in flight
at once (a warning above 12).

`Main(oracle)` is the oracle's command line, the task's `[accuracy] command`:

```text
ORACLE --history DIR [--out DIR] [--partition-timeout D] [--total-timeout D]
       [--max-search-steps N] [--memory-mb N] [--workers N]
       [--max-reads-per-key N] [--max-ops-per-partition N]
       [--max-lost-share F] [--seed N]
```

It exits 0 on PASS, 1 on FAIL and 2 on UNKNOWN, prints the report, writes
`report.txt` and the failures' timelines to `--out` (default
`/vibesys/artifacts/accuracy`), and prints the verdict's one-paragraph
reason on standard error when it is not PASS.

Its `import` command (`WriteHistory`) makes a history directory of loadgen
phase directories captured before the accuracy capture wrote histories,
with the faults between them, to check old runs; it exits 0, or 2 with the
reason:

```text
ORACLE import --out DIR [--dataset FILE] [--fault SPEC]... PHASE_DIR...
```

A `SPEC` is `kind=K,after=N[,target=T][,start=TIME][,end=TIME]` (`TIME` in
Unix nanoseconds or RFC 3339); a fault without times is placed between the
last response of phase `N` and the first call of the next. The phase
directories' files are hard-linked, or copied.

### Accuracy history

The accuracy capture writes one directory per run, which `OpenHistory`
reads:

```text
DIR/history.binpb            HistoryManifest: phases, faults, dataset
DIR/<dataset>                the dataset file the generator read
DIR/phase-<i>/results.binpb  loadgen's results: every body kept, client set
                             (or results.binpb.gz, as a phase's artifacts)
DIR/phase-<i>/summary.binpb  loadgen's summary (the operation names)
DIR/phase-<i>/kept.binpb     a KeptOperation for every operation (its info)
```

- Phases are `phase-0` to `phase-<phases - 1>`, each run by loadgen at the
  task's fixed load with every `PlannedOperation.keep_bodies` set.
- `OperationResult.client` is the loadgen client that ran the operation (in
  an open loop, the in-flight slot); two operations of one client never
  overlap. The checker uses it for the timelines' lanes.
- `kept.binpb` holds the generator's `KeptOperation`s, length-delimited
  without a header, one for every operation (its `info_json`); a phase pod
  leaves them in `state/kept.binpb`, which the reader also accepts.
- `HistoryManifest.faults` are the `FaultEvent`s between phases: a kind
  (`restart`, `restart_all`, `flush`, ... the oracle's `Fault` gives it
  meaning), a target, the phase it follows, and its start and end on the
  clock loadgen stamps its results with (the phase pod's). No operation may
  be in flight from a fault's start to its end; the checker reports one
  that is as a capture problem (UNKNOWN).
- Operations never sent (start 0) are not in the history. An operation with
  a step that got no response (status 0) is `Call.Lost`.
- A directory without `history.binpb` (today's phase directories) is read
  with the phases it has, no faults, and `dataset.json` if present.

### Accuracy run

A task's accuracy check is one command of its script, the same flow in Go
and Python:

```text
bench.py accuracy [--project-root DIR] [--out DIR] [--seed N]
TASK_BINARY accuracy [--project-root DIR] [--out DIR] [--seed N] [--history DIR]
```

1. The dataset file goes into `OUT/history/` and the application is
   deployed and seeded as for a benchmark sample (a namespace of its own on
   Kubernetes, deleted on every path).
2. `phases` open-loop phases of the check's load run through the phase
   runner with capture on: every response body, the loadgen client of each
   operation and the generator's `info`. Each phase pod runs `phase
   --accuracy`, whose generator is the check's own workload
   (`Accuracy(workload=...)`, Go `Accuracy.Workload` or `Generator`) when it
   has one and the task's otherwise, so one script serves a benchmark's mix
   and a check's. No phase is judged as a measurement: neither the
   saturation guard nor the validity rules apply (a workload may send
   requests the application must refuse), and each phase logs what it sent
   and how many of its operations failed; the oracle judges every response.
3. After phase `i`, the faults with `after == i` run in order, each recorded
   as a `FaultEvent` with its start and end on the loadgen clock. `restart`
   deletes one pod of the Deployment (the first by name), `restart_all` every
   pod at once, and both wait until it serves again (replicas available, the
   ready steps through the gateway); `exec` runs a command in the first pod,
   and a nonzero exit ends the run; `flush` empties a cache Deployment (one
   of `k8s.App(caches)`) in place, in every pod. A fault that cannot be
   applied (a Kubernetes fault on a local application) ends it too. Each wait
   is bounded by `k8s.App(wait_secs)`.
4. The application is torn down, then the oracle judges `OUT/history`: in
   Go, the `Accuracy.Oracle` specs in-process (`check.Check`); in Python, the
   oracle program with `--history OUT/history --out OUT`.

The exit status is the verdict's: 0 PASS, 1 FAIL, 2 UNKNOWN, which is also
the status of a run that could not complete (a deploy, a phase or a fault
that failed, a signal), with `UNKNOWN: the accuracy run did not complete:
...` on standard error. `OUT` (default `/vibesys/artifacts/accuracy`, or
`accuracy/` in `$VIBESYS_ARTIFACTS_DIR`) holds `report.txt`, the failures'
timelines and the history. The Go `--history DIR` judges a history again
without running. The Python wrapper drives the core's `accuracy run`.

## Core command line (`vibesys-evaluator`)

The Go module's `cmd/vibesys-evaluator` is the core the language SDKs wrap, one
subcommand per coarse operation: `vibesys-evaluator <group> <verb>`. One rule
for every subcommand:

- the request is one protobuf-JSON message on stdin (defined in `proto/`), the
  response one protobuf-JSON message on stdout;
- progress and logs go to stderr, which the wrapper passes through;
- exit 0 is OK; exit 1 is a domain error, and the response carries
  `error_kind` and `message`; any other code means the core itself broke;
- a long-running verb streams newline-delimited JSON events on stdout and ends
  with the response;
- each group registers its verbs in its own file under
  `go/cmd/vibesys-evaluator/` (`register(group, verb, handler)` from `init`).

Python calls them through one helper, `_private/core.py`: it finds the binary
(the wheel's `_bin/`, then `$VIBESYS_EVALUATOR_BIN`, then `PATH`), runs it,
decodes the reply and raises `CoreDomainError` (exit 1), `CoreCrashError` (any
other exit, or a reply that is not JSON) or `CoreNotFoundError`. `loadgen` and
`otelcapture` are found the same way (`$VIBESYS_EVALUATOR_LOADGEN`,
`$VIBESYS_EVALUATOR_OTELCAPTURE`). See "Distribution".

The verbs (messages in `proto/vibesys/evaluator/v1/core.proto`, and
`microservices.proto` for `phase`):

| Verb | What it does |
|---|---|
| `core version` | what this build implements |
| `sample start` | resolve the sample seed (`--seed`, `$VIBESYS_SAMPLE_SEED`, else drawn) and the artifacts directory; write `seeds.json` |
| `sample phase` | create one phase's directory, `<artifacts>/<name>`, refusing a bad or repeated name |
| `sample combine` | combine each metric's trials into the sample's value (median, mean, min, max, sum) |
| `records write` | append one result-protocol-2 record (`hello`, `result` or `error`) to the `--vs-output` file, enforcing the protocol against what the file holds |
| `hist summary` | merge HDR histograms and read percentiles from the sum |
| `phase pod` | run one phase where the phase pod runs: loadgen and the generator, the pod's files (long: `{"event": PhaseEvent}` lines, then the response) |
| `phase judge` | whether a phase's files are a measurement: the saturation guard and the validity rules |
| `accuracy run` | an accuracy check: deploy and seed the application (Kubernetes, or a local one), run the phases with capture (not judged as measurements) and the faults between them into the history directory, tear it down, run the oracle program (long: `{"event": PhaseEvent}` lines, then the verdict) |

Error kinds a wrapper sees: `record` (a record the protocol refuses),
`histogram`, `phase`, `invalid_argument`, `bad_request`, `io`, `cancelled`
(a signal stopped a long verb), `deploy` and `run` (an accuracy run's
deploy, or a phase or fault of it, failed).

The logic is the Go package `evaluator` (`go/evaluator`), which task authors
can use directly: `evaluator.Main(evaluator.Task{Metrics, Benchmark, Check,
Profile})` is the Go `main` (the same command line, exit codes and records as
the Python one; the functions receive a `context.Context` that the first
SIGTERM, SIGINT or SIGHUP cancels, and a `*Run` with `Phase`, `Report`, `Seed`
and `SetPartial`), `Metric` and `Validity` are the same plain data,
`Stream` writes the records, `MergeHistograms` and `Histogram.Percentile` read
HDR histograms, `Combine` aggregates trials, and `PhaseDir`, `SampleSeed` and
`WriteSeeds` are the sample's files. `examples/go-bench` is a whole benchmark
in Go.

The Python wrappers (`main`, `Run`, `Metric`, `Validity`) keep their API. What
stays Python is what has to: the control flow around the task's own Python
functions (calling them, catching their exceptions and signals) and the plain
data classes, whose rules the cross-language fixtures `fixtures/core.json`
pin to Go's (record streams byte for byte, histogram summaries and merges,
validity and seed rules); `Rand` is native in both languages and pinned by
`fixtures/rand.json`. `main` runs the `vibesys-evaluator` binary, which the
platform wheels bundle (see "Distribution").

## Distribution

`pip install vibesys-evaluator` is the whole install: the PyPI release holds
one wheel per platform (linux x86_64 and aarch64, macOS x86_64 and arm64), each
with the Go binaries `vibesys-evaluator`, `loadgen` and `otelcapture` in
`vibesys_evaluator/_bin/`, the way `ruff` bundles its binary. The binaries are
built with `CGO_ENABLED=0`, so the Linux wheels are static and carry the
manylinux and the musllinux tags at once; macOS wheels are cross-compiled.
`packaging/evaluator_wheels.py` builds and verifies them.

The release also holds a source distribution, which has no binary, and no
`py3-none-any` wheel. An installation without a bundled binary (the sdist, or a
platform without a wheel, such as Windows) finds the binary in
`$VIBESYS_EVALUATOR_BIN` (`..._LOADGEN`, `..._OTELCAPTURE` for the others), then
on `PATH`; failing that, a task script exits 1 with a message that names those
variables and the `go install` command. The Go module stays installable by
`go install github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/cmd/<binary>@<tag>`
for authors who write in Go.

The task images install the linux x86_64 wheel and link its binaries onto the
`PATH`; they run no Go toolchain.

## Generator command line

The phase pod runs every generator, Python or Go, alike:

```text
GENERATOR generate --phase-input F --dataset F --state DIR
```

- `GENERATOR` is `python bench.py` for a Python workload (its `generate`
  command), the `microservices.Generator` command, or a Go task binary of
  `microservices.Main` with its `Workload`. In an accuracy check's phases
  (`phase --accuracy`) with a workload of the check's own, it is `python
  bench.py accuracy-workload` (or `TASK_BINARY accuracy-workload`) for a
  Python (Go) one, or the check's `Generator` command.
- `generate` writes the request stream (`FileHeader` kind requests,
  `RequestsHeader`, then `PlannedOperation`s, binpb) to standard output until
  the stream is closed, then exits 0. Under capture (`PhaseInput.capture`)
  every operation keeps its bodies, and the generator keeps a
  `KeptOperation` (its info) for every operation in `--state/kept.binpb`,
  written before the operation, so every operation loadgen read has its
  record. Standard error carries only logs. It never ends the stream itself.
- There is no `check` command: a benchmark only measures.
- Exit 0 on success, 2 for a usage error, 1 for any other failure (a `build`,
  `begin_phase` or dataset error, naming the operation).

Where the examples show the hard parts:

- Multi-step template: `examples/kv-store/bench.py` (`login`: log in, capture
  the token, send it).
- The larger tasks are the `hotel-k8s` and `social-network-k8s`
  tasks under `.vibesys/tasks/` in the DeathStarBench fork
  (`vibesys-playground/DeathStarBench`, `vibesys` branch), which the
  repository's `examples/microservices/repositories/deathstarbench` submodule
  checks out.

## loadgen command line

```text
loadgen closed --target URL --clients N --secs S --warmup W
               --results F --summary F [--timeout S] [--buffer N]
               [--record-clients] [--format binpb|json] -- GENERATOR [ARG...]
loadgen open   --target URL --rate R [--arrivals uniform|poisson] [--seed N]
               [--max-in-flight N] [--min-sustained RATIO] --secs S --warmup W
               --results F --summary F [--timeout S] [--buffer N]
               [--record-clients] [--format binpb|json] -- GENERATOR [ARG...]
loadgen probe  --target URL --requests F --rounds N --deadline S
               [--timeout S] [--format binpb|json]
loadgen selftest --summary F [--step-secs S] [--max-rate R] [--accept-any-rate] [--format binpb|json]
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
- `--record-clients` sets `OperationResult.client`: the closed loop's client,
  or the open loop's in-flight slot, that ran the operation (the accuracy
  capture's; two operations of one client never overlap).
- `--arrivals` defaults to `poisson`; `uniform` spaces arrivals `1/rate`
  apart, as the yardsticks do.
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
- `phase.proto`: `PhaseInput` (sample seed, phase index, load, capture),
  `PhaseOutput` (loadgen's summary and run time), `KeptOperation` (under
  capture, a generator's record of every operation and its info, in
  `--state`), and the accuracy history's `HistoryManifest` and `FaultEvent`
  (see "Accuracy history"). `HistoryEntry`, `Violation` and the fields of
  the checks are no longer written.
- `microservices.proto`: the core's `phase pod`, `phase judge` and
  `accuracy run`.

Length-delimited binary is the default; `--format json` writes proto3 JSON,
one message per line. Histograms are microseconds in the standard compressed
HdrHistogram V2 encoding, which both the Go and Python libraries read (the
raw bytes, not their base64 text). Conformance fixtures
([`fixtures/`](../fixtures), indexed by `fixtures/manifest.json`) hold files
of each kind that both sides must read and write identically, and files a
reader must reject.

Sizes: a request of the yardsticks encodes in about 100 to 300 bytes; loadgen
holds only its buffer (8,192 operations, a few MB), whatever the phase's
length or rate. Bodies are kept as 40 bytes (SHA-256 and length) except under
capture. A phase pod killed at its memory limit (OOMKilled) fails the sample
with an error that says so and names the limit; what to lower is the
generator's model, since loadgen's own memory is bounded by its buffer.

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

`Validity` is the task's: `max_error_rate` (loadgen's errors over the
window's operations), `min_sustained_ratio` (open loop: offered rate
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

Every request of a sample is a function of the dataset and the sample seed.

## Deploy layer (`microservices.k8s.App`)

Generic over the application, from hotel-k8s' and social-network-k8s'
run.py: the cluster comes from the vibesys-cli `kubernetes` resource
(`KUBECONFIG`, `VIBESYS_K8S_REGISTRY`, `VIBESYS_K8S_BUILDER`: the fields of
`KubernetesEnvironment`), and the job's time limit from `VIBESYS_JOB_TIMEOUT_SECONDS`
(`JobEnvironment.timeout_seconds`), which the Go core reads with the SDK's own reader of those
messages (`proto/vibesys/cli/v1/env.proto`, `docs/contributing/cli-task-environment.md`); a missing
or malformed variable is refused naming it. The core reads no task manifest. The sweep deletes
the namespaces an earlier job left behind once they are older than the running job's limit plus
ten minutes; jobs of one cluster run one at a time (the `kubernetes` resource's exclusive claim).

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
- `restartable`, `caches`: the Deployments an accuracy check's faults may act on
  (`Fault.restart`, `Fault.restart_all` and `Fault.exec` on either, `Fault.flush`
  on a cache; see "Accuracy run"). Outside an accuracy check, the same actions go
  through `python bench.py ctl restart DEPLOYMENT [--every-pod] | flush CACHE | stop | start`
  or, from Python, `k8s.attach(app)` (the sample's namespace, by default
  `$VIBESYS_EVALUATOR_NAMESPACE`): `restart(*deployments, every_pod=False,
  wait=True)` deletes one pod (the first by name) or every pod and, with `wait`,
  waits for all replicas and the probes; `exec(argv, deployment=... | pod=...,
  all_pods=False)` runs a command in a pod (a nonzero exit is a result, not an
  error); `wait_ready(deployments, replaced=...)` waits until the app serves
  again; `flush(cache)` runs `FLUSHALL` or `flush_all` in place; `stop()` and
  `start()` scale every restartable Deployment to 0 and back. Only Deployments
  listed in `restartable` or `caches` can be restarted or exec'd in.
- `wait_secs` (Go `App.WaitSecs`; default 300): how long each wait of the
  deploy may take: for the data tier, for every replica, for a pod to start, and
  for a restarted Deployment to serve again (a fault's wait too). An application
  whose pods load a large snapshot before they start needs more.
- Phase pods: one pod per phase in the namespace, the inputs copied in and
  the outputs copied out with `kubectl exec ... tar`, so no ConfigMap size limit
  applies; pods of the package carry its labels and die with the namespace.

### The Go core of the deploy layer

The logic is the Go package `go/k8s` (`github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s`),
which runs `kubectl` and `buildctl` as subprocesses behind the `KubeAPI` and `Builder` interfaces
(`k8s/k8stest` holds an in-memory cluster and builder, so the package is tested without a cluster).
Python's `k8s.App` keeps its shape and its constructor-time checks; everything it does on a
cluster is one call of `vibesys-evaluator k8s <verb>` (the wrapper calling convention; messages in
`proto/vibesys/evaluator/v1/k8s.proto`):

| Verb | Does |
| --- | --- |
| `deploy` | sweep this task's stale namespaces, build the images, deploy into a fresh namespace, seed, probe; returns the `target` |
| `attach` | check a namespace is the app's; returns its `target` |
| `teardown` | delete the namespace (`fast` after a signal; `failed` saves diagnostics) |
| `run-pod` | run a command of the task image in a pod, with input files, and collect its outputs |
| `restart` | delete one or every pod of Deployments; wait for the replacements unless `no_wait` |
| `exec` | run a command in a pod, or in the pods of a Deployment |
| `wait` | wait until the app serves again (optionally without the pods `replaced_uids`), then probe |
| `flush`, `stop`, `start`, `usage` | empty a cache; scale the restartable Deployments; per-pod CPU |

The core keeps no state between calls: `target` (the app, namespace, UID, task image and scratch
directories) travels in every request. `deploy` and `run-pod` log progress on stderr and are
cancelled by SIGINT or SIGTERM, after which `deploy` deletes its namespace without waiting.
Failures are the convention's domain errors with the kinds `deploy`, `build`, `manifest`,
`invalid`, `out_of_memory`, `kube` and `canceled`.

## LLM serving (`llm_serving`)

The candidate image runs an OpenAI-compatible server; vibesys-cli starts it,
waits for its ready path and sets `VIBESYS_SERVICE_URL` in the task
container to its base URL (scheme, host and port, no path) for the
`[accuracy]` and `[benchmark]` commands of a `version = "0.2"` task, and for
a command job in the task image (`submit command --image harness`). The task container has no GPU; nothing the candidate installs reaches
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
(`sdk/vibesys-evaluator/go/vX.Y.Z`) and the images, one per domain:
`vibesys-evaluator-microservices:X.Y.Z` (the package, and loadgen from its wheel) and
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
The public Go API is `api/go-workload.txt`, `api/go-check.txt` and
`api/go-microservices.txt` (`go doc -all` of `go/workload`, `go/check` and
`go/microservices`), checked against the code (a Go test each) and against the last release (no declaration of them may change or go
away).
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
