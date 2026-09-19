# Running VibeSys

A repository-native VibeSys run operates on one candidate repository and one
named task. The repository root is the agent and evaluator working directory.
Human-authored task inputs live below `.vibesys/tasks`; generated state lives
below `.vibesys/state`.

```text
project/
├── .git/
├── .vibesys/
│   ├── tasks/
│   │   └── <task>/
│   │       ├── OBJECTIVE.md
│   │       ├── vibesys.input.toml
│   │       ├── Dockerfile           # optional task execution environment
│   │       ├── accuracy_checker/    # optional task-owned program
│   │       ├── benchmark/           # optional task-owned program
│   │       └── reference/           # optional held-out inputs
│   └── state/
│       ├── project.json
│       ├── runs/<run-id>/          # committed run metadata and loop state
│       └── local/runs/<run-id>/worktrees/ # temporary evolve worktrees only
└── candidate source

~/.vibesys/projects/<project-key>/
├── current-run
└── runs/<run-id>/                  # logs and machine-local state
```

VibeSys commits candidate evolution and portable `.vibesys/state/runs/`
metadata on a `vibesys-runs/<run-id>` branch. Machine-local metadata defaults to
`~/.vibesys`; set `VIBESYS_STATE_HOME` to an absolute directory to override it.
Existing `.vibesys/state/local/` metadata moves there on first open without
changing its file formats. Temporary evolve worktrees remain repository-local.
The entire `.vibesys/` directory is read-only to coding agents.

## Start a Task

Launch from the candidate repository. Select the task explicitly when the
repository defines more than one:

```bash
cd /path/to/project
vibesys validate --task latency
vibesys --task latency --max-rounds 4
```

`--project` selects the repository explicitly. `--input` remains a compatibility
alias during migration:

```bash
vibesys --project /path/to/project --task latency \
  --config /path/to/project/agent.toml
```

The task name may be omitted when `.vibesys/tasks` contains exactly one task.
The repository must be its Git root, or outside any Git repository so VibeSys
can initialize one. An existing repository needs a baseline commit and a clean
worktree. Keep `agent.toml` and root `.env*` files out of Git history.

When the selected repository-native task contains a conventional
`.vibesys/tasks/<task>/Dockerfile`, VibeSys builds it with that task directory
as the complete build context and automatically selects Docker execution. The
resolved immutable image ID is recorded with the run. Docker's native layer
cache makes later launches incremental. The candidate repository is mounted at
runtime and is never part of the image build context.

Task Dockerfiles are intentionally convention-based: they require no manifest
field or image-build command. A task Dockerfile cannot be combined with another
image source or a different run environment. `--docker` and
`--run-environment docker` are redundant but accepted. Legacy root input
bundles do not opt in through a root `Dockerfile`, because that file commonly
belongs to the candidate application.

All outer loops use this model. Local, Docker, and Modal execution change where
commands run, not the task layout. Task
commands always start in the repository root. `.vibesys` is mounted read-only
for coding agents. Machine-local state is outside their workspace.

Modal tasks may set a project-relative deployment file. Omit this block to use
the legacy `main.py` default:

```toml
[environment.modal]
entrypoint = "examples/deployment/service.py"
```

Accuracy and benchmark commands may be task-owned argv arrays, or stable entry
points supplied by an exact evaluator package:

```toml
version = 1

[agent]
domain = "generic"

[evaluator]
name = "vibesys-evaluator-queue"
version = "0.1.0"

[accuracy]
entrypoint = "vibesys-queue"
args = ["check", "--workspace", "${PROJECT_ROOT}", "--scenario", "spsc"]

[benchmark]
entrypoint = "vibesys-queue"
args = ["benchmark", "--workspace", "${PROJECT_ROOT}", "--scenario", "spsc"]
```

## Container Topologies

A `microservices` candidate is not a process the agent starts on its own: it is
a Docker Compose topology the agent must build, start, and trace. Such a task
needs Docker Engine reachable without `sudo` and, when its benchmark shells out
to Go helpers, a Go toolchain on `PATH`.

A local run in this domain therefore imports two domain-specific host resources
the default confinement withholds: the Docker control socket, and the task
scratch directory `/tmp/vibesys-<task>`. The scratch directory is shared with the host
rather than masked by the sandbox's private `/tmp`, because Docker resolves a
bind-mount source in the daemon's namespace, not the agent's, so a capture
directory only resolves when the path names the same directory inside and
outside confinement. Sharing it also makes the benchmark's telemetry artifacts
durable and readable by the profiler in later rounds. Reaching the Docker
socket is equivalent to root on the host, so this widening is scoped to this
domain; use `--docker` to confine the workload to a container instead.

A benchmark command that names both `--telemetry-output` and
`--trace-graph-json` declares that the task provisions instrumentation and a
collector. `--profiler auto` selects the OpenTelemetry profiler for such a task,
which gives the loop service, span, datastore, critical-path, and trace
breakdown evidence. Tasks without those flags stay on `none`, since OTel
profiling has nothing to read.

## Legacy Input Bundles

Root-level `OBJECTIVE.md` plus `vibesys.input.toml`, `[workspace]`,
`evaluator.source`, standalone `--input-*` flags, and `--runs-dir` copied
projects remain temporarily supported for examples that have not migrated.
New integrations should use repository-native tasks. A task in a standalone
repository runs in place. A repository-shaped example nested below another Git
root uses `--runs-dir` to materialize an isolated project.

For example:

```bash
vibesys \
  --runs-dir /work/vibesys-runs \
  --local \
  --input /path/to/input
```

VibeSys provisions a self-contained project below the runs directory. Candidate
source is at the copied project's root, and a declared evaluator is copied
below `_evaluator/`. The source input's `.git/`, `.vibesys/`, `agent.toml`, and
`.env*` files are not copied. Omit `--local` to publish the copied project using
the authenticated GitHub CLI. See [Legacy bundles](cli-flags.md#legacy-bundles)
for the source, evaluator, and publication contracts.

## Resume

Inside a project, resume the machine-local current run from the configured
VibeSys state home, then the newest run if no current pointer exists. A run ID
selects a specific run:

```bash
cd /path/to/project
vibesys --resume
vibesys --resume <run-id>
```

With `--runs-dir`, the resume argument selects a legacy copied project in the
collection. It may be a directory name, a local path, a GitHub `OWNER/NAME`, or
a cloneable URL. VibeSys then resumes that project's current or newest run.

```bash
vibesys --runs-dir /work/vibesys-runs --resume <project-directory-name>
vibesys --runs-dir /work/vibesys-runs --resume /path/to/project
vibesys --runs-dir /work/vibesys-runs --resume my-org/my-experiment
vibesys --runs-dir /work/vibesys-runs --resume https://github.com/my-org/my-experiment.git
```

Omitted configuration flags and the selected task are restored from
`.vibesys/state/runs/<run-id>/run.json`. The total round or generation limit
may increase. Other recorded settings cannot change during a resume, including
the runtime environment: a run launched with `--modal` resumes on Modal without
repeating the flag.

For a SkyPilot run, the portable environment choice is recorded, while the
operator profile and local profiles-file path must be supplied on the resuming
host. Nonterminal evaluator identity and replayable results live in the run's
machine-local `skypilot` state namespace. See
[Remote Slurm execution](remote-slurm-execution.md).

## Supply an Input with CLI Flags

Automation can supply the input contract without first writing an input
directory. This form requires `--runs-dir`. VibeSys synthesizes the objective
and manifest, then provisions the same canonical project layout.

```bash
vibesys \
  --runs-dir /work/vibesys-runs \
  --local \
  --input-objective-file ./OBJECTIVE.md \
  --input-domain generic \
  --input-accuracy-command "python check.py" \
  --input-benchmark-command "python benchmark.py" \
  --input-benchmark-metric throughput \
  --input-benchmark-result-arg=--output-json \
  --input-evaluator-dir ./evaluation
```

Use this form for CI or programmatic integrations. Checked-in
`OBJECTIVE.md` and `vibesys.input.toml` files are easier to review for projects
maintained by people.

## Presentation and Validation

- `vibesys` launches the TUI when attached to a terminal.
- `vibesys --headless` disables the TUI.
- Non-interactive execution runs headless automatically.
- `vibesys validate [PROJECT] --task NAME` checks the static task contract
  without starting an agent or executing the checker and benchmark.

Legacy root input bundles remain valid positional arguments without `--task`.

See the [CLI reference](cli-flags.md) for every flag and
[`examples/`](https://github.com/uw-syfi/vibesys/tree/main/examples) for complete objectives and manifests.
