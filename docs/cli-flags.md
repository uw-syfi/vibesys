# CLI Flags and Supported Combinations

This document is the canonical map for VibeSys's CLI flag axes. Update it in
the same PR whenever a flag, backend, domain, loop, runtime environment, or
profiler behavior changes.

## Mental Model

Several flags look independent, but they combine into one execution contract:

| Axis | Flag | Meaning |
| --- | --- | --- |
| Search loop | `--outer-loop` | Which outer-loop policy runs: `agent`, `plain`, `evolve`, or `openevolve`. |
| Evaluation interface | `--interface` | Agent loop only. Whether evaluator-owned code invokes the candidate directly or communicates with a service. |
| Compute backend | `--backend` | Hardware/runtime target: `cuda`, `metal`, `trainium`, or `cpu`. |
| Runtime environment | `--docker`, `--modal` | Where agent commands execute: local shell, Docker container, or Modal-backed workflow. |
| Profiler | `--profiler` | Bottleneck evidence source: `nsys`, `torch`, `neuron`, `macos_cpu`, `linux_cpu`, or `auto`. |
| Domain | `[agent].domain` in `vibesys.input.toml` | Agent-loop problem-space package, such as `llm-serving` or `generic`. |
| Modality | `--modality` | Per-task I/O contract, such as `text_generation` or `speech_to_text`. |
| Skills | `--skills-dir`, `--no-skills` | Candidate skill roots and the ablation switch that disables skill loading. |
| Target inputs | `--input` | Target bundle directory with manifest-declared correctness and benchmark commands. |

Do not treat these as simple toggles. Some combinations imply a startup
contract, profiler, or sandbox capability. Language and artifact requirements
come from the domain and input bundle, not the interface mode.

## Outer Loops

| Value | Behavior | Notes |
| --- | --- | --- |
| `agent` | Orchestrator-driven loop with implementer, judge, and profiler roles. | Default. Supports `--interface` and `--inner-loop`. |
| `plain` | Issue-board loop with deterministic issue draining and perf evaluation. | Uses backend prompt fragments from `src/vibesys/templates/_backend/`. |
| `evolve` | Evolutionary search over candidate implementations. | Uses mutator, judge, and profiler roles. |
| `openevolve` | MAP-Elites-style evolutionary loop. | Reuses evolve mutator, judge, and profiler prompts. |

From a source checkout, use `./vs` for the commands below. It prepares a current
interactive client when needed and forwards every argument to the TypeScript
launcher. For installed npm users, the same launcher is exposed as `vs` and
`vibesys`.
Use `./vs --outer-loop <kind> --help` for loop-specific flags.

## Repository Validation

Run `vibesys validate` from a configured repository to check its static VibeSys
contracts without starting the interactive client, an optimization loop, or an
agent. From a source checkout, use the equivalent `./vs validate` command.

With no flags, validation reads `agent.toml` and treats the current directory as
the target input bundle:

```bash
vibesys validate
```

Repositories that keep the bundle elsewhere can select both files explicitly:

```bash
vibesys validate --config agent.toml --input examples/<target>
```

The command applies the same strict schemas and path checks as a real run. It
validates the agent config, `OBJECTIVE.md`, `vibesys.input.toml`, accuracy and
benchmark command paths, optional workspace seed and evaluator source, and the
optional benchmark-result contract. A passing repository exits with status 0;
an invalid repository prints the failing contract and exits with status 1.
Command-line usage errors exit with status 2. Validation does not execute the
checker or benchmark and does not probe external tools or credentials.

## Interface

`--interface` applies to the agent loop.

| Value | Process boundary | Contract ownership |
| --- | --- | --- |
| `inprocess` | Evaluator-owned code invokes the candidate directly inside an evaluator process. | The input defines the callable API or ABI, artifacts, ownership, and lifecycle. |
| `service` | Checker and benchmark communicate with a running candidate over its network interface. | The input defines the protocol, endpoints, startup behavior, and artifacts. |

`service` does not automatically rewrite a checker or benchmark. The target
inputs must already know how to probe the running service.

`inprocess` does not imply Python. A Python module imported by an accuracy
checker and a C-ABI shared library loaded by a trusted adapter are both
in-process candidates. Their exact requirements belong to domain/use-case
prompts and input-owned candidate-contract documentation.

## Compute Backends

| Backend | Intended target | Sandbox support | Device handling | Default profiler behavior |
| --- | --- | --- | --- | --- |
| `cuda` | NVIDIA GPU serving systems. | Local, Docker, Modal. | Selects/reselects a GPU and can monitor contention. | Local/Docker use `nsys`; Modal uses `torch` when `--profiler auto`. |
| `metal` | Apple Silicon / MPS targets. | Local only. | No device selection or monitor. | Local `auto` resolves through the local runtime default. |
| `trainium` | AWS Trainium / NeuronCore targets. | Local and Docker; Modal unsupported. | Forwards `/dev/neuron*` in Docker; no per-device selection. | `auto` resolves to `neuron`. |
| `cpu` | CPU-only service/data-structure targets. | Local and Docker. | No device selection or monitor. | Generic workloads on Linux select `linux_cpu`; macOS selects `macos_cpu`; other systems select no profiler. |

When a backend rejects a runtime environment, it should fail before agent work
starts with an actionable error.

## Runtime Environment

| Flags | Environment | Notes |
| --- | --- | --- |
| neither `--docker` nor `--modal` | Local shell. | Commands run on the host through `LocalShellBackend`. |
| `--docker` | Docker container. | Mounts the workspace. Backend controls GPU/device passthrough. |
| `--modal` | Modal workflow. | Mutually exclusive with `--docker`. Intended for remote GPU dispatch. |

`--docker-image` overrides the backend's default container image when Docker or
Modal is active.

## Profiler

| Value | Intended use |
| --- | --- |
| `auto` | Let the runtime/backend pick the default profiler. |
| `nsys` | NVIDIA Nsight Systems. Requires a CUDA/NVIDIA profiling environment. |
| `torch` | PyTorch profiler. Used for in-process Python profiling and Modal GPU dispatch. |
| `neuron` | AWS Neuron profiler for Trainium. |
| `macos_cpu` | Instruments Time Profiler with a supported `/usr/bin/sample` fallback. |
| `linux_cpu` | Linux `perf` profiler for native and mixed-language CPU workloads. |

`--modal --profiler nsys` is rejected by the CLI because Modal runs must use the
torch profiler path.

Profiler prompts must match the interface, domain, and backend. In-process
execution alone does not make the candidate Python or PyTorch-compatible; the
selected domain must explicitly support Torch profiling. A CPU backend must not
receive a GPU-kernel workflow.

The macOS backend verifies that the selected developer directory is full Xcode and asks
`xctrace` for the Time Profiler template; the Command Line Tools shim is not considered
functional Instruments. Captures are separate diagnostic runs, never scored results.
They store exact commands, duration, warm-up, OS/CPU/tool data, target PID/topology,
diagnostics, and the raw `.trace` or `sample` report. Attach failures, including SIP or
privacy restrictions, are structured diagnostics. Optimized native builds should retain
debug information; `dsymutil`, `dwarfdump`, `nm`, and `atos` can validate or resolve
symbols. Reports must state when unavailable Apple hardware counters limit conclusions.

## Domain and Modality

`[agent].domain` in `vibesys.input.toml` supplies cross-cutting problem-space
context for the agent loop. Registered domains include:

| Domain | Meaning |
| --- | --- |
| `llm-serving` | LLM-serving guidance, including serving-system skills and judge gates. |
| `generic` | No extra domain guidance. Useful for custom/non-LLM targets. |

Each input bundle must declare `[agent].domain`; there is no CLI override. New
domains are added in source by registering a domain package with optional
environment setup/teardown hooks.

`--modality` supplies the task I/O contract, such as text generation or
speech-to-text. Domains and modalities may define language, toolchain, and
artifact requirements. Interface-specific prose should describe only the
direct-call or service boundary.

## Skills

`--skills-dir` supplies skill candidate roots. Each value may point at one skill
directory containing `SKILL.md`, or at a parent tree containing multiple skills.
The default candidate root is `resources/skills/`.

Before a run starts, VibeSys discovers each `SKILL.md` under the candidate
roots and validates its frontmatter. Optional `.vibesys.toml` sidecars can
declare backend applicability for a skill subtree:

```toml
[[rule]]
path = "skills"
backends = ["trainium"]
```

Effective skill loading is:

- backend-agnostic skills load for every `--backend`;
- skills matched by a sidecar rule with `backends` load only when the selected
  backend is in that list;
- `--skills-dir` adds candidate roots, but backend metadata still filters the
  discovered skills;
- `--no-skills` disables all skill loading, including backend-scoped skills.

See [Skill Metadata](skill-metadata.md) for the VibeSys-specific metadata
contract and validation rules.

## Target Inputs

Most examples use the standard bundle layout:

```text
examples/<target>/
├── OBJECTIVE.md
├── vibesys.input.toml
├── reference/
├── accuracy_checker/
└── benchmark/
```

For nontrivial callable APIs, ABIs, ownership rules, or service protocols, keep
the normative implementation requirements in `CANDIDATE_CONTRACT.md` and link
to it from `OBJECTIVE.md`. A shared evaluator may own this file when several
input bundles use exactly the same contract. Keep evaluator internals and trust
assumptions in a separate design document.

For those bundles, pass the root once:

```bash
./vs --input examples/<target> ...
```

The manifest declares the evaluator entrypoints and does not define a candidate
command:

```toml
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["uv", "run", "python", "accuracy_checker/checker.py"]

[benchmark]
command = ["uv", "run", "python", "benchmark/benchmark.py"]

[workspace]
seed = "../../starters/example-rust-candidate"

[evaluator]
source = "../../evaluators/example"

[benchmark.result]
json_argument = "--output-json"
metric = "requests_per_second"
```

Those command arrays are bundle-specific. They may point at Python, shell, Go,
Rust, C++, or any other evaluator entrypoint, and VibeSys does not require
standard wrapper filenames. VibeSys copies the input bundle into the
experiment workspace and tells agents to run the manifest commands. The
optional `benchmark.result` block opts a single-metric benchmark into trusted
framework scoring: VibeSys appends `json_argument`, reads the resulting JSON,
and requires exactly one numeric field named by `metric`. Omit it for
multi-profile or multi-objective benchmarks whose result cannot be represented
by one scalar. Named profiles and benchmark parameter schemas are not part of
manifest version 1.

The optional `workspace.seed` path is relative to the input manifest and must
resolve inside the repository's `examples/starters/` directory. On a fresh run,
VibeSys copies non-ignored seed files first and then copies the input bundle.
Any top-level path supplied by both sources is rejected instead of being
overwritten. The resulting files are ordinary candidate workspace files: agents
may edit or delete them, and resumed runs never refresh them from the seed.

The optional `evaluator.source` path is relative to the input manifest and must
resolve inside `examples/evaluators/`. On a fresh run, VibeSys copies it to
`_evaluator/<source-name>`. This is a separate, evaluator-owned input: Git-backed
integrity checks reject accuracy and benchmark gates after it is modified.
Resumed runs keep the evaluator snapshot from the original run instead of
refreshing it from repository source.

## Common Commands

Default agent loop on local CUDA-compatible host:

```bash
./vs \
  --outer-loop agent \
  --backend cuda \
  --interface inprocess \
  --input examples/model-serving/Llama-3-8B
```

Docker CUDA run:

```bash
./vs --outer-loop agent --backend cuda --docker ...
```

Modal GPU run:

```bash
./vs --outer-loop agent --backend cuda --modal --profiler torch ...
```

Trainium run:

```bash
./vs --outer-loop agent --backend trainium --profiler auto ...
```

Over-the-wire service target:

```bash
./vs \
  --outer-loop agent \
  --interface service \
  --input examples/<target>
```

CPU-only target in the current merged code:

```bash
./vs --outer-loop agent --backend cpu --interface service ...
```

Use local execution unless the CPU backend's Docker support is present in your
checkout.

## Maintenance Rule

When adding or changing a flag:

1. Update this document.
2. Add or update validation for unsupported combinations.
3. Add prompt-rendering tests for combinations that change generated
   instructions.
4. Keep README focused on quickstart guidance and link here for details.
