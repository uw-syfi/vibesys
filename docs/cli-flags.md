# CLI Flags and Supported Combinations

This document is the canonical map for VibeSys's CLI flag axes. Update it in
the same PR whenever a flag, backend, domain, loop, runtime environment, or
profiler behavior changes.

Start with [Running VibeSys](running-vibesys.md) for the project layout and the
choice between editing the input project or provisioning a copy. This document
is the exhaustive flag reference.

## Entry Point

Use the installed `vibesys` command. It launches the interactive TUI by default
and runs headless with `--headless`, `--help`, `validate`, or when not attached
to a TTY. Examples below use this command.

### Agent configuration

`--config PATH` selects an explicit `agent.toml`. When the flag is omitted,
VibeSys loads `agent.toml` from the process launch working directory if it
exists, otherwise it uses built-in CLI defaults. It does not search parent
directories.

## Mental Model

Several flags look independent, but they combine into one execution contract:

| Axis | Flag | Meaning |
| --- | --- | --- |
| Search loop | `--outer-loop` | Which outer-loop policy runs: `agent`, `plain`, or `evolve`. |
| Evaluation interface | `--interface` | Agent loop only. Whether evaluator-owned code invokes the candidate directly or communicates with a service. |
| Compute backend | `--backend` | Hardware/runtime target: `cuda`, `metal`, `trainium`, `rocm`, or `cpu`. |
| Runtime environment | `--docker`, `--modal` | Where agent commands execute: local shell, Docker container, or Modal-backed workflow. |
| Profiler | `--profiler` | Bottleneck evidence source: `nsys`, `torch`, `neuron`, `otel`, `macos_cpu`, `linux_cpu`, or `auto`. |
| Domain | `[agent].domain` in `vibesys.input.toml` | Problem-space package used by the agent and evolve loops, such as `llm-serving`, `microservices`, or `generic`. |
| Modality | `--modality` | Per-task I/O contract, such as `text_generation` or `speech_to_text`. |
| Skills | `--skills-dir`, `--extra-skills`, `--no-skills` | Override the preset skill roots, stack extra skills on top of the presets, or disable skill loading. |
| Target project | `--project` (`--input` alias) | Candidate repository. Defaults to the current directory. |
| Task | `--task NAME` | Task below `.vibesys/tasks`; optional when exactly one exists. |
| Project provisioning | `--runs-dir PATH` | Copy a legacy bundle or a repository-shaped example nested below another Git root. Standalone repository-native projects run in place. |
| Project repository | `--repo`, `--repo-visibility`, `--local`, `--resume` | Fresh copied projects use GitHub by default. Resume also accepts local project paths or GitHub repositories. |
| Client theme | `--theme` | Presentation only. Which semantic theme the interactive client renders with. |

Do not treat these as simple toggles. Some combinations imply a startup
contract, profiler, or sandbox capability. Language and artifact requirements
come from the domain and input bundle, not the interface mode.

## Outer Loops

| Value | Behavior | Notes |
| --- | --- | --- |
| `agent` | Orchestrator-driven loop with implementer, judge, and profiler roles. | Default. Supports `--interface` and `--inner-loop`. |
| `plain` | Issue-board loop with deterministic issue draining and perf evaluation. | Uses backend prompt fragments from `src/vibesys/prompts/backend/`. |
| `evolve` | Evolutionary search over candidate implementations. | Uses domain-aware mutator, judge, and profiler roles. |

Run the commands below with `vibesys`.
Use `vibesys --outer-loop <kind> --help` for loop-specific flags.

## Canonical Projects

The candidate repository is the working project. `--project` defaults to the
current directory:

```bash
cd /path/to/project
vibesys --task latency

# Equivalent without changing directory
vibesys --project /path/to/project --task latency
```

The project must contain `.vibesys/tasks/<name>/OBJECTIVE.md`,
`.vibesys/tasks/<name>/vibesys.input.toml`, and the candidate source the agent
will edit. A task name is required when the repository defines multiple tasks.
It must be the root of its Git repository, or outside any Git repository so
VibeSys can initialize one. A subdirectory of a containing Git repository is
not a valid project root. An existing repository must have a baseline
commit and a clean worktree. A directory outside Git is initialized with a
baseline commit automatically.

The `agent`, `plain`, and `evolve` loops, runtime environments, profilers, and
agent backends all use the same project layout. A repository-shaped example
nested below another Git root may use `--runs-dir` to materialize an isolated
project. Legacy bundles and standalone `--input-*` synthesis also use this
compatibility path.

The native sandbox requires `bwrap` on Linux or `sandbox-exec` on macOS. A
missing confinement tool or an unsupported operating system stops the run
rather than launching the agent without isolation. On Linux `bwrap` must also
be able to create a user namespace; a present but blocked binary is treated as
missing and reported at startup rather than failing once per round. Evaluator,
checker, and benchmark paths are visible to agents but enforced read-only and
protected by integrity checks. The agent also cannot write `.git`, `.vibesys`,
legacy root-level task inputs, or `_evaluator/`, and cannot read
`.vibesys/state/local/`, root `.env*` files, or root `agent.toml`. A run is
rejected when a root `.env*` file or `agent.toml` is recoverable from Git refs
or reflogs.

`VIBESYS_AGENT_SANDBOX` selects the Linux mechanism. `auto` (the default) and
`bwrap` both require bubblewrap. `landlock` opts in to a weaker backend for
hosts that block unprivileged user namespaces, which is the common reason
bubblewrap cannot run without root (for example Ubuntu's
`kernel.apparmor_restrict_unprivileged_userns`). The Landlock backend keeps the
outer boundary — the project is writable and the rest of the host is denied for
both read and write — but Landlock rules only ever add access, so it cannot
carve a restriction out of the writable project. Read-only and hidden project
paths are therefore *not* enforced under it, and each unenforced tier is logged
at startup. Evaluator-input integrity still holds, because the accuracy gate
independently diffs those paths against a trusted baseline and fails the round.
Landlock is never selected automatically, and a project that sits inside a tree
the backend must grant (such as `/tmp`) is refused rather than run unconfined.
Set the variable to `0`/`false`/`off`/`no` to disable confinement entirely; any
other value is rejected.

VibeSys initializes Git when needed and creates one `vibesys-runs/<run-id>` branch
per run. Agent-authored source stays at its normal project paths. Portable and
machine-local state are separated under `.vibesys/state/`:

```text
.vibesys/
└── state/
    ├── .gitignore                         # contains /local/
    ├── project.json                       # committed project identity
    ├── runs/<run-id>/
    │   ├── run.json                       # committed sanitized run configuration
    │   ├── agent/rounds/NNNN.json         # agent-loop completed rounds
    │   ├── plain/                          # plain-loop portable cursor
    │   ├── evolve/                         # evolve population and policy state
    │   └── runtime/effective-objective.md  # objective plus run constraints
    └── local/                             # ignored operational state
        ├── current-run
        └── runs/<run-id>/
            ├── agent/active.json
            ├── round-transaction.json      # during round commit/recovery
            ├── logs/
            └── worktrees/                  # temporary evolve worktrees
```

The committed files contain portable configuration, fingerprints, metrics, and
round outcomes. They exclude provider credentials, environment variables,
absolute source paths, sessions, and raw logs. Resume restores the saved branch
and run configuration:

```bash
# Resume the run named by .vibesys/state/local/current-run, or newest if unset
vibesys --resume

# Select a run explicitly
vibesys --resume <run-id>
```

A plain `vibesys` launch starts a new run. It does not silently resume the
current or latest run. When run flags and `--config` are omitted during resume,
their recorded values are restored. Explicit configuration changes are
rejected, except that `--max-rounds` may increase the recorded total. The project
worktree must be clean before VibeSys switches to the saved
`vibesys-runs/<run-id>` branch.

### Agent-loop review and memory policy

| Flag | Default | Behavior |
| --- | ---: | --- |
| `--judge-every N` | `3` | Run an independent judge every Nth round. A candidate explicitly nominated by the implementer and the final round are always reviewed immediately. Canonical accuracy and benchmark commands run only after a judge PASS. |
| `--official-eval-every N` | `3` | Run configured framework-owned accuracy and benchmark gates every N accepted candidate checkpoints. Intermediate checkpoints remain provisional; orchestrator requests and the final round force immediate official evaluation. Retries, continuing hypotheses, and profiler-only rounds do not advance this cadence. Modal gates reuse one healthy deployment for the exact candidate commit, explicitly stop it after the final gate, and rely on zero minimum-warm replicas plus a short finite scaledown window as the crash backstop. Unchanged retries reuse a prior accuracy PASS when only a later gate failed. |
| `--memory-layout` | `files` | `files` keeps `roadmap.md` and `progress.md`. `directories` uses `roadmap/index.md` and one `progress/round-NNNN.md` audit file per round; fresh orchestrators receive a bounded recent window and can inspect older files on demand. Existing runs retain their current layout when resumed. |
| `--constraint TEXT` | none | Add an operator-supplied workload invariant to every agent's objective without changing the input bundle. The framework commits the effective objective under the run's portable `.vibesys/state/` and mounts it read-only in isolated environments, so candidate edits cannot erase it. Repeat for multiple constraints and repeat the same flags when resuming. |

Designer and judge invocations start with clean model sessions. The implementer
session is keyed by the designer's stable `hypothesis_id`, so targeted
experiments and debugging context persist while the causal claim is unchanged.
The designer is not called again while that hypothesis is active. A round
reported as `continue` is provisional when review is not due: neither the judge
nor framework-owned official gates run that round. These independent-judge
cadence rules apply to the default `multi-agent` inner loop; `single-agent` is
retained as an ablation mode.

### Evolve search policies

`--search-policy vibesys` (the default) uses VibeSys's scalar softmax or Pareto
frontier selection. `--search-policy openevolve` imports pinned OpenEvolve 0.3.1
and delegates MAP-Elites archiving, island selection, population limits, and
migration to its `ProgramDatabase`. It does not use OpenEvolve's one-shot LLM
mutation path; VibeSys's coding agent continues to mutate the checked-out
multi-file project.

| Flag | Default | Meaning under `--search-policy openevolve` |
| --- | ---: | --- |
| `--openevolve-population-size` | `1000` | Maximum upstream program population. |
| `--openevolve-archive-size` | `100` | Elite archive size. |
| `--openevolve-num-islands` | `5` | Number of island populations sampled round-robin. |
| `--openevolve-migration-interval` | `50` | Per-island admitted generations between migrations. |
| `--openevolve-migration-rate` | `0.1` | Fraction of island elites copied during migration. |

OpenEvolve state is stored under
`.vibesys/state/runs/<run-id>/evolve/openevolve/` and loaded on resume. See
[`docs/contributing/openevolve.md`](contributing/openevolve.md) for the adapter boundary and metric
semantics. On resume the policy and saved settings are restored when these
flags are omitted; partial explicit settings are merged with the saved values,
flag-defined objectives are restored, and incompatible changes are rejected.
On a new run, supplying any
`--openevolve-*` setting also selects the OpenEvolve policy.

## Copied Project Collections and Remote Repositories

Configure interactive defaults in `agent.toml`:

```toml
[repository]
# Optional GitHub user/org override. If omitted, use the account authenticated with `gh`.
# owner = "your-github-user"
visibility = "private"
```

Runs created with `--runs-dir` use GitHub by default. The owner can be any
GitHub user or organization; when `owner` is omitted, VibeSys uses the account
authenticated with `gh`. Interactive and headless launchers pass the same
arguments directly to the engine, so provide the input and collection
explicitly. Names default to `<input-name>-<UTC timestamp>`.

For headless use, the generated repository name is used automatically. Pass
`--repo NAME` to override the name, or `--repo OWNER/NAME` to override the owner
explicitly. Pass `--local` to keep a copied project in its collection without a
GitHub repository. A direct project stays local unless `--repo` is explicit.
Repositories use `[repository].visibility` unless `--repo-visibility`
overrides it. Creation goes through the authenticated `gh` CLI.

The project repository records candidate history and portable
`.vibesys/state/runs/` state. Provider and agent logs under
`.vibesys/state/local/` are excluded. On context shutdown, VibeSys pushes the
already-authored run branch and retained evolve candidate refs. Publication
never stages files or creates a synchronization commit. A non-fast-forward or
authentication failure is reported rather than force-pushing.

With `--runs-dir`, `--resume` accepts collection project names, local project directories,
GitHub `OWNER/NAME` pairs, and cloneable HTTPS/SSH URLs:

```bash
vibesys --runs-dir /work/vibesys-runs --resume 20260720-120000-example
vibesys --runs-dir /work/vibesys-runs --resume /work/experiments/example
vibesys --runs-dir /work/vibesys-runs --resume vibesys-playground/example
vibesys --runs-dir /work/vibesys-runs --resume https://github.com/my-org/example.git
```

Remote repositories are cloned into the selected collection. A local clone can
live anywhere, but `--runs-dir` is still required for shared caches and any
future runs. Resumed repositories with an `origin` are synchronized again after
the run. `--repo` only creates a repository for a fresh experiment and cannot be
combined with `--resume`.

## Repository Validation

Run `vibesys validate [PROJECT] --task NAME` to check a task's static harness
contract without starting the interactive client, an optimization loop, or an
agent.

The project is the positional argument. When omitted, it defaults to the current
directory. `--task` may be omitted only when one task exists:

```bash
vibesys validate
```

Pass another project directly:

```bash
vibesys validate examples/model-serving/repositories/vllm \
  --task llama-3-8b-h100-long-prompts
```

The command applies the same strict schemas and path checks as a real run. It
validates the task objective and manifest, accuracy and benchmark command
paths or package entry points, and the optional benchmark-result contract. A
valid task exits with status 0; an invalid task prints the failing contract
and exits with status 1. Command-line usage errors exit with status 2.
Validation does not execute the checker or benchmark.
Legacy root input bundles remain valid positional arguments without `--task`.

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
| neither `--docker` nor `--modal` | Local host. | Requires bubblewrap on Linux or Seatbelt on macOS. Enforces the project path policy. `VIBESYS_AGENT_SANDBOX=landlock` trades the nested read-only and hidden tiers for a backend that runs without user namespaces. |
| `--docker` | Docker container. | Mounts the project with the same hidden and read-only overlays. Backend controls GPU/device passthrough. |
| `--modal` | Modal workflow. | Mutually exclusive with `--docker`. Intended for remote GPU dispatch. |

`--docker-image` overrides the backend's default container image when Docker or
Modal is active.

The selected environment and its options (`--docker-image`, `--modal-gpu`,
`--modal-model-volume`, `--modal-app`) are recorded in the run configuration.
`--docker` and `--modal` are boolean flags, so an omitted flag cannot be told
apart from an explicit "off": on resume the recorded environment is therefore
authoritative. Omitting the runtime-environment flags restores it, and passing
a flag that contradicts the recording is rejected like any other immutable
configuration field. The candidate's Modal entrypoint is declared by the task,
not recorded, so it follows the current input bundle.

Run metadata written before run schema version 2 has no recorded environment.
VibeSys refuses to load it rather than guess a local environment. Stamp the
environment the run was launched with, once:

```bash
vibesys migrate-run-environment --project . --run <run-id> --run-environment modal
```

The command accepts the same `--docker-image`, `--modal-gpu`,
`--modal-model-volume`, and `--modal-app` options as a run, with the same
defaults, and is one-way.

## Profiler

| Value | Intended use |
| --- | --- |
| `auto` | Let the runtime/backend pick the default profiler. |
| `nsys` | NVIDIA Nsight Systems. Requires a CUDA/NVIDIA profiling environment. |
| `torch` | PyTorch profiler. Used for in-process Python profiling and Modal GPU dispatch. |
| `neuron` | AWS Neuron profiler for Trainium. |
| `otel` | OpenTelemetry service, span, and datastore latency for microservice benchmarks. Opt-in only (`auto` never selects it) and needs an input bundle that provisions instrumentation and a collector. |
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
context for the agent and evolve loops. Registered domains include:

| Domain | Meaning |
| --- | --- |
| `llm-serving` | LLM-serving guidance, including serving-system skills and judge gates. |
| `microservices` | Microservice workload guidance, lifecycle rules, and service-level evaluation context. |
| `generic` | No extra domain guidance. Useful for custom/non-LLM targets. |

Each input bundle must declare `[agent].domain`; there is no CLI override for a
bundle passed with `--input`. When synthesizing a bundle from `--input-*` flags
instead, `--input-domain` sets `[agent].domain` for the generated manifest. New
domains are added in source by registering a domain package with optional
environment setup/teardown hooks.

`--modality` supplies the task I/O contract, such as text generation or
speech-to-text. Domains and modalities may define language, toolchain, and
artifact requirements. Interface-specific prose should describe only the
direct-call or service boundary.

## Client Theme

`--theme` selects the semantic theme the interactive client renders with. It is
presentation only: it never reaches the agents, the project, or the recorded
run state, and it is ignored in headless mode.

| Theme | Appearance | Use for |
| --- | --- | --- |
| `dark` | dark | Default dark palette. |
| `light` | light | The baseline palette inverted for light terminals. |
| `solarized-dark` | dark | Low-glare Solarized palette. |
| `solarized-light` | light | Low-glare Solarized palette. |
| `catppuccin-mocha` | dark | Softer, more expressive palette. |
| `catppuccin-latte` | light | Softer, more expressive palette. |
| `high-contrast-dark` | dark | Accessibility-focused; every foreground clears a 7:1 contrast ratio. |
| `high-contrast-light` | light | Accessibility-focused; every foreground clears a 7:1 contrast ratio. |

Resolution order, highest first:

1. `--theme <name>` on the command line.
2. `dark`.

An unknown name is rejected before any process starts. Inside a running
session, `/theme` opens the theme list as a keyboard selection (Up/Down to
move, Enter to apply, Escape to close without switching) and `/theme <name>`
switches immediately; that switch applies to the session only and does not edit
`agent.toml`.

Themes define semantic roles — surfaces, text emphasis levels, borders,
accents, status colors, conversation roles, and Markdown/code colors — rather
than per-component colors, and every derived foreground is checked against its
own background at build time. Status is never carried by color alone: agent
phases show a marker glyph and the spelled-out status, todo items show a
per-status marker, and the running round is the only one with an elapsed-time
suffix.

```bash
vibesys --runs-dir /work/vibesys-runs --local \
  --input examples/model-serving/Llama-3-8B --theme solarized-light
```

## Skills

Skill sources come from two flags, both repeatable, and each value may point at
one skill directory containing `SKILL.md`, a parent tree containing multiple
skills, or a single `SKILL.md` file:

- **`--skills-dir PATH`** *replaces* the built-in preset roots. When omitted, the
  preset `resources/skills/` is the base.
- **`--extra-skills PATH`** *stacks on top of* the presets (or on top of
  `--skills-dir` when that is given). Use this to add your own skills while
  keeping the presets such as the `llm-serving` serving-systems skills. A
  same-named skill from `--extra-skills` overrides a preset one.

```bash
# presets + your own skill directory and a single SKILL.md file
vibesys --runs-dir /work/vibesys-runs --local --input <bundle> \
  --extra-skills ./my-skills \
  --extra-skills ./one-off-skill/SKILL.md

# use ONLY your skills, ignoring the presets
vibesys --runs-dir /work/vibesys-runs --local \
  --input <bundle> --skills-dir ./my-skills
```

Before a run starts, VibeSys discovers each `SKILL.md` under the candidate
roots and validates its frontmatter. Optional `.vibesys.toml` sidecars can
declare domain and backend applicability for a skill subtree:

```toml
[[rule]]
path = "skills"
backends = ["trainium"]
domains = ["llm-serving"]
```

Effective skill loading is the intersection of the declared constraints:

- unscoped skills load for every domain and `--backend`;
- skills matched by a sidecar rule with `backends` load only when the selected
  backend is in that list;
- skills matched by a sidecar rule with `domains` load only when the input
  bundle's `[agent].domain` is in that list;
- `--skills-dir` and `--extra-skills` add candidate roots, but routing metadata
  still filters the discovered skills;
- `--no-skills` disables all skill loading, including scoped skills, and
  overrides both `--skills-dir` and `--extra-skills`.

See [Skill Metadata](contributing/skill-metadata.md) for the VibeSys-specific metadata
contract and validation rules.

## Target Inputs

Repository-native examples use this layout:

```text
candidate-repository/
├── .vibesys/
│   └── tasks/<task>/
│       ├── OBJECTIVE.md
│       ├── vibesys.input.toml
│       ├── reference/
│       ├── accuracy_checker/
│       └── benchmark/
└── candidate source
```

For nontrivial callable APIs, ABIs, ownership rules, or service protocols, keep
the normative implementation requirements in `CANDIDATE_CONTRACT.md` and link
to it from `OBJECTIVE.md`. A shared evaluator may own this file when several
input bundles use exactly the same contract. Keep evaluator internals and trust
assumptions in a separate design document.

Launch from its root or select the root and task explicitly:

```bash
cd /path/to/project
vibesys --task <task> ...

vibesys --project /path/to/project --task <task> ...
```

The manifest declares direct commands or logical entry points from one exact
evaluator package. Both run from the candidate repository root:

```toml
version = 1

[agent]
domain = "generic"

[accuracy]
entrypoint = "vibesys-queue"
args = ["check", "--workspace", "${PROJECT_ROOT}", "--scenario", "spsc"]

[benchmark]
entrypoint = "vibesys-queue"
args = ["benchmark", "--workspace", "${PROJECT_ROOT}", "--scenario", "spsc"]

[evaluator]
name = "vibesys-evaluator-queue"
version = "0.1.0"

[benchmark.result]
json_argument = "--output-json"
metric = "requests_per_second"
```

Direct `command = [...]` arrays may point at Python, shell, Go, Rust, C++, or
any other task-owned program. Package entry points decouple task manifests
from the package's install location. `${PROJECT_ROOT}` in package arguments
expands to the absolute candidate repository root.

The optional `benchmark.result` block opts a single-metric benchmark into
trusted framework scoring: VibeSys appends `json_argument`, reads the resulting
JSON, and requires a finite numeric field named by `metric`. For a JSON object,
that field must be at the top level and is authoritative even when per-trial
diagnostics repeat the same name. List-shaped results are accepted when they
contain exactly one field with that name. Omit the result block for
multi-profile or multi-objective benchmarks whose result cannot be represented
by one scalar. Named profiles and benchmark parameter schemas are not part of
manifest version 1.

`benchmark.result_protocol` is the alternative, and the two are mutually
exclusive. It declares that the benchmark speaks the evaluator result protocol
of that version, so VibeSys reads a complete metric row instead of scraping one
named field:

```toml
[benchmark]
entrypoint = "vibesys-queue"
args = ["benchmark", "--workspace", "${PROJECT_ROOT}", "--scenario", "spsc"]
result_protocol = 2
```

VibeSys appends `--vs-output` with a path, and the benchmark writes a record
stream there: a `hello` record declaring every metric it produces, then one
`result` record carrying their values, or an `error` record explaining why it
could not measure. The framework validates the row against the declaration and
fails the round when they disagree, when a value is missing or not finite, or
when a configured objective names a metric the benchmark does not produce. The
protocol is specified in
[sdk/vs-evaluator/PROTOCOL.md](https://github.com/uw-syfi/vibesys/blob/main/sdk/vs-evaluator/PROTOCOL.md),
and `sdk/vs-evaluator/vseval` is a Go SDK for emitting it. Use this form for
multi-objective benchmarks, which the scalar block cannot represent.

Task resources, including held-out evaluation sets, remain at their repository
paths. VibeSys does not relocate them. `.vibesys` is read-only to coding agents,
so task commands must write scratch data outside `.vibesys` (for example under
`/tmp` or a candidate-excluded build directory).

For `--modal` runs, a task may declare the candidate deployment file:

```toml
[environment.modal]
entrypoint = "examples/deployment/service.py"
```

The entrypoint is relative to the candidate repository root, must remain inside
that repository, and defaults to `main.py` when omitted. The manifest supplies
task-owned deployment wiring; the operator still selects Modal with `--modal`.

### Legacy bundles

Unmigrated examples may still use root-level `OBJECTIVE.md` and
`vibesys.input.toml`, `workspace.sources`, and `evaluator.source`. These require
`--runs-dir` when materialization is needed. They are a compatibility path, not
the format for new repository adoption.

The optional `evaluator.source` path is relative to the input manifest and must
resolve to a directory. On a fresh run, VibeSys copies it to
`_evaluator/<source-name>`. This is a separate, evaluator-owned input:
Git-backed integrity checks reject accuracy and benchmark gates after it is
modified. Resumed runs use the evaluator snapshot committed in the canonical
project.

### Providing inputs without a bundle (`--input-*`)

For external usage where no `examples/` bundle is on disk, pass the bundle's
contents as separate `--input-*` flags instead of `--input`. VibeSys synthesizes
a bundle under `<runs-dir>/_inputs/<exp-name>/` and then loads it through the
same path as `--input`, so every loop, resume, and evaluator behaves identically.
The two forms are mutually exclusive: combining `--input` with any `--input-*`
flag is rejected.

Required flags:

| Flag | Maps to |
| --- | --- |
| `--input-objective TEXT` or `--input-objective-file PATH` | `OBJECTIVE.md` |
| `--input-domain {llm-serving,generic,microservices}` | `[agent].domain` |
| `--input-accuracy-command CMD` | `[accuracy].command` (shell-quoted argv) |
| `--input-benchmark-command CMD` | `[benchmark].command` (shell-quoted argv) |

Optional flags:

| Flag | Maps to |
| --- | --- |
| `--input-accuracy-timeout SECONDS` / `--input-benchmark-timeout SECONDS` | command `timeout_seconds` |
| `--input-benchmark-metric NAME` + `--input-benchmark-result-arg OPT` | `[benchmark.result]` (both required together) |
| `--input-reference DIR` | copied to `reference/` |
| `--input-evaluator-dir DIR` | contents copied into the bundle root (evaluator scripts the commands invoke) |
| `--input-evaluator-source DIR` | `[evaluator].source` (staged inside the bundle) |

The synthesized bundle stages `--input-evaluator-source` inside itself before
loading the manifest, so the supplied local directory does not need to share a
parent repository with the generated bundle. Git-pinned
`[[workspace.sources]]` entries are not exposed as flags; use `--input` for
those.

```bash
vibesys \
  --runs-dir /work/vibesys-runs \
  --input-objective-file ./OBJECTIVE.md \
  --input-domain llm-serving \
  --input-accuracy-command "python checker.py" \
  --input-benchmark-command "python benchmark.py" \
  --input-benchmark-metric requests_per_second \
  --input-benchmark-result-arg=--result-json \
  --input-evaluator-dir ./evaluator \
  --local
```

## Common Commands

Default agent loop on local CUDA-compatible host:

```bash
vibesys \
  --runs-dir /work/vibesys-runs \
  --local \
  --outer-loop agent \
  --backend cuda \
  --interface inprocess \
  --input examples/model-serving/Llama-3-8B
```

Docker CUDA run:

```bash
vibesys --runs-dir /work/vibesys-runs --local \
  --outer-loop agent --backend cuda --docker ...
```

Modal GPU run:

```bash
vibesys --runs-dir /work/vibesys-runs --local \
  --outer-loop agent --backend cuda --modal --profiler torch ...
```

Trainium run:

```bash
vibesys --runs-dir /work/vibesys-runs --local \
  --outer-loop agent --backend trainium --profiler auto ...
```

Over-the-wire service target:

```bash
vibesys \
  --runs-dir /work/vibesys-runs \
  --local \
  --outer-loop agent \
  --interface service \
  --input examples/<target>
```

CPU-only target:

```bash
vibesys --runs-dir /work/vibesys-runs --local \
  --outer-loop agent --backend cpu --interface service ...
```

CPU runs support local execution and Docker; use local execution unless you
specifically need the container boundary.

## Maintenance Rule

When adding or changing a flag:

1. Update this document.
2. Add or update validation for unsupported combinations.
3. Add prompt-rendering tests for combinations that change generated
   instructions.
4. Keep README focused on quickstart guidance and link here for details.
