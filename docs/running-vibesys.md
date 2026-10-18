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
~/.vibesys/cache/<architecture>/
└── evaluator-tools/<tool>/<spec-digest>/  # evaluator tools, shared by all projects
```

VibeSys commits candidate evolution and portable `.vibesys/state/runs/`
metadata on a `vibesys-runs/<run-id>` branch, cut from the branch you had checked
out when the run started. A run that adopts a winner commits it on that branch,
naming the candidate and what it measured; your own branch is never changed, so
merge the run branch to keep the winner. The run's branch stays checked out after
it ends; the next run checks your branch out again first, so each run starts from your own
work. Machine-local metadata defaults to
`~/.vibesys`; set `VIBESYS_STATE_HOME` to an absolute directory to override it.
Existing `.vibesys/state/local/` metadata moves there on first open without
changing its file formats. Temporary evolve worktrees remain repository-local.
The entire `.vibesys/` directory is read-only to coding agents, and its
`state/runs/` and `state/local/` are hidden from them: the run's record is the
framework's, and `state/agent-visible/` holds the few documents an agent reads
by path.

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
as the complete build context, on top of the backend's default image (passed
as the build argument `BASE_IMAGE`, so the Dockerfile may start with
`ARG BASE_IMAGE` and `FROM ${BASE_IMAGE}`), and layers the agent on top. Every
run environment whose agent runs in a local container does this the same way:
Docker (the default), and the agent container of Modal, SkyPilot and Slurm.

The run records the task image as a local tag, `vibesys-task-build:<owner>-<digest>`.
`<owner>` identifies the task directory and is used only to clean up that
directory's older tags; a build never removes another task's tags. `<digest>`
covers the base image and every file the build sends to Docker: the
Dockerfile, `.dockerignore`, and the build context minus what `.dockerignore`
excludes. Only `<digest>` decides whether the inputs changed, so a project
copied elsewhere (as `--runs-dir` does) resumes. The run itself uses the
image's immutable ID, read once at launch, so a later launch that removes the
tag cannot affect a running run's agent containers. On resume:

- if the recorded image is still in the local Docker daemon, it is reused and
  nothing is built. If the inputs changed since the run started (an edited
  benchmark, say), VibeSys prints a notice and keeps the recorded image;
- otherwise, if the inputs are unchanged, VibeSys rebuilds the image and
  continues, with a notice (the image ID may differ when Docker's build cache
  has been pruned);
- if the image is gone and the inputs changed, or the run was recorded without
  a task image the task now declares, resume refuses. A run recorded with the
  removed `local` environment and continued in Docker falls in the second case.

Runs recorded before tags held a bare image ID: they are reused while that ID
is local, and otherwise rebuilt as they were then (without `BASE_IMAGE`) and
refused unless the rebuild reproduces the recorded ID. Docker's native layer
cache makes later launches incremental. The candidate repository
is mounted at runtime and is never part of the image build context.

Task Dockerfiles are intentionally convention-based: they require no manifest
field or image-build command. A task Dockerfile cannot be combined with
`--docker-image`, and the `host` environment (Metal on Apple Silicon) refuses it,
because it has no container to build it into. A root `Dockerfile` never opts
in, because that file commonly belongs to the candidate application.

On Modal, SkyPilot and Slurm the task image is the agent's container only;
evaluation runs where those environments run it. With Slurm and
`[vibesys.agent_gpu]`, `vibesys-gpu` forwards the agent container's
environment variables into each GPU job on the compute node
(`libs/vs-sandbox/src/vs_sandbox/host_command_client.py` sends them,
`job_environment` in `host_command_broker.py` keeps all but the container's
identity variables such as `PATH` and `HOME`). An `ENV` line in the task
Dockerfile therefore leaks into GPU jobs, where it may name paths that exist
only in the image. Put settings the job needs somewhere it reads them on both
sides instead, such as a `.pth` file in the project's virtual environment, and
keep image `ENV` to values that are correct on the compute node too.

All outer loops use this model. The agent always runs in a local Docker
container. Modal, SkyPilot, and Slurm change where evaluation runs, not the task
layout. The Slurm environments reach the cluster through a host-owned broker
that the container connects to over a mounted socket. Task commands always start in the repository root. `.vibesys` is mounted read-only
for coding agents. Machine-local state is outside their workspace.

Modal tasks may set a project-relative deployment file. Omit this block to use
the legacy `main.py` default:

```toml
[environment.modal]
entrypoint = "examples/deployment/service.py"
```

A task declares how a candidate is measured in one of two forms. The short
form names the stages `build` (optional), `check` and `measure`, and the
headline `metric`:

```toml
version = 1
metric = "total_ops_per_sec"

[agent]
domain = "generic"

[evaluator]
name = "vibesys-evaluator-queue"
version = "0.1.0"

[build]
command = ["make", "queue-candidate.so"]
timeout_seconds = 600

[check]
entrypoint = "vibesys-queue"
args = ["check", "--workspace", "${PROJECT_ROOT}", "--scenario", "spsc"]

[measure]
entrypoint = "vibesys-queue"
args = ["benchmark", "--workspace", "${PROJECT_ROOT}", "--scenario", "spsc"]
```

The existing form names the same check and measure stages `[accuracy]` and
`[benchmark]`, and may declare `[build]` too. Both forms compile to the same
stages; declaring one stage in both forms is refused. A short-form `measure`
speaks evaluator result protocol 2 unless it declares a `result` table.

Check, measure and diagnostic profile commands may be task-owned argv arrays,
or stable entry points supplied by an exact evaluator package; they are trusted
evaluator code. The build is candidate code: an argv run from the root of a
fresh checkout of the measured commit, confined like the agent (in the Docker
run environment, the candidate's container). After it passes, VibeSys verifies
every file the evaluator package requires (`candidate_outputs` in its
`vibesys.evaluator.toml`, for example `queue-candidate.so`); a missing one
fails the build stage naming it. The verified files are kept outside every
workspace and placed at their declared paths in the checkouts where the check
and the measure run, which receive their workspace-relative paths, `:`-joined,
in `VIBESYS_CANDIDATE_OUTPUTS`. A commit is built once: a later stage or
measurement of the same commit reuses its build. A task whose evaluator requires
outputs but declares no build fails `vibesys preflight` and the desktop app's
New run check. The build needs agent confinement, so the host run environment
refuses it, and the Slurm evaluation executor does not run build stages yet (#2968).

For trusted serving captures, also declare a bundle-owned diagnostic workload:

```toml
[profile]
command = ["python", "benchmark/profile.py", "--base-url", "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1"]
timeout_seconds = 120
```

The workload is short and fixed, exercises requests without benchmark preflight
or warmup gates, and lives in trusted evaluator inputs. It follows the same
command-or-entrypoint contract as the other task commands. A configured serving
capture requires this declaration and names `profile.command` when it is absent.
The timeout bounds the load after readiness and cannot exceed the remaining
capture deadline. Profiling provides diagnostic evidence; accuracy and benchmark
acceptance still use their own unchanged gates.

A task may name workspace paths its evaluation never reads, such as the notes
an agent keeps between turns:

```toml
[workspace]
unread = ["NOTES.md", "evidence/"]
```

A commit that changes only these paths is the input an earlier run already
measured, so its runs count as samples of that input and the strategy's
official measurement can reuse them instead of submitting another job. Each
entry is a plain relative file or directory path, without wildcards.

### Measurements, samples and comparison with the base

Every measurement runs the task's gates through the run's executor on a committed
revision, and is trusted whoever asked for it: the agent's evaluation tool, the
strategy's final check, or a re-run. Each conclusive run of one measured input is
a sample of one measurement, recorded with its commit, the measured-input digest,
its base commit, the gates, the host, and whether the executor held the host alone
(`dedicated_measurement_resources`; a Slurm executor declares it with that setting
in its configuration, or with a bare `--exclusive` as the last node-sharing option
in `sbatch_arguments`; `--exclusive=user`, `=mcs`, `=topo`, `--oversubscribe`, `--share` and
`-s` share nodes and do not count). A host held alone lowers the noise; a shared host is
recorded and its spread shows in the noise, but it is not refused.

Samples of one input are aggregated: nothing is picked among them and nothing is
refused because there are several, so re-running an input until a run suits the
agent gains nothing. The strategy's official measurement of an input the attempt
already measured is answered from those samples, and runs afresh only when a
comparison with the base is a close call that one more sample would narrow.

An agent's evaluation is compared with its base, the revision its workspace
started from. The base is measured once per (measured input, gates, host) and
cached in the run's state, and measured again only when its samples are older
than the task's `base_staleness` on the run clock (6 hours) or a close call earns
it one more sample (a base holds at most two fresh samples). The
comparison is the ratio of the candidate's mean over the base's mean, its noise,
and a verdict: `better`, `within_noise` or `worse`. The noise is one standard
error of the ratio, computed from the runs' relative spread (never below the
task's declared `relative_noise`) and the sample counts; a verdict needs two
noises. The agent's evaluation tool and the resumed turn report it, and the run's
events carry it as `measurement_compared`.

## Start from an Example

The examples under `examples/<category>/` in a VibeSys source checkout are
project roots in this layout. Runs never use them in place: copy one into a
new repository first.

```bash
vibesys example list                     # categories, examples, and their tasks
vibesys example new data-structures/stack ~/work/stack
cd ~/work/stack
vibesys --task spsc
```

`example new` copies the files the checkout tracks under the example, runs
`git init`, makes one initial commit, and prints the `vibesys --task ...` line
to run. The destination must be absent or an empty directory, outside the
checkout. An example registered with `external_repo = true` is cloned from its
repository at the commit the checkout pins instead. A unique last path
component (`stack`) also selects an example. `vibesys example list --json`
prints one `ExampleList` document (`entrypoints.example_catalog`), which the
desktop app reads.

## Container Topologies

A `microservices` candidate is not a process the agent starts on its own: it is
a Docker Compose topology the agent must build, start, and trace. Such a task
needs Docker Engine reachable without `sudo` and, when its benchmark shells out
to Go helpers, a Go toolchain on `PATH`.

A `microservices` task therefore opts in to a Docker daemon of its own with
`docker_in_docker = true`:

```toml
[environment]
docker_in_docker = true
```

The key selects the Docker run environment automatically. Modal, SkyPilot, or
Slurm with the key is an error that names it. Without the key a task gets no container runtime at all: VibeSys
never mounts or imports the host's Docker socket.

### Docker-in-Docker

The agent container runs under the Sysbox runtime (`docker run --runtime
sysbox-runc`, never `--privileged`) and VibeSys starts `dockerd` inside it
before the first turn, waiting until `docker info` answers. The agent can
`docker compose up --build` or create kind clusters inside the sandbox. The
daemon's images, containers, and volumes live in the sandbox container and
disappear with it, and trusted evaluator commands run in the same container, so
they use the same daemon. The sandbox is CPU-only: Sysbox cannot forward
accelerators.

The workspace is mounted at the same path inside the container as on the host
(not at `/workspace`), so a compose file's relative or absolute bind sources
mean the same directory to the agent and to the daemon; `/tmp` is the
sandbox's own. The agent image gains the Docker engine, the compose plugin,
kind, and kubectl only for these tasks (the `container-runtime` toolchain;
versions are pinned in `vs_agent.provider_policy`), so ordinary tasks' images
do not grow.

If the host's Docker daemon does not register the `sysbox-runc` runtime, the
run fails before building any image, naming the missing runtime and the key.
There is no fallback to the host socket or to a plain container.

A benchmark command that names both `--telemetry-output` and
`--trace-graph-json` declares that the task provisions instrumentation and a
collector. `--profiler auto` selects the OpenTelemetry profiler for such a task,
which gives the loop service, span, datastore, critical-path, and trace
breakdown evidence. Tasks without those flags stay on `none`, since OTel
profiling has nothing to read.

## Compute Backend

A task declares the compute backends it targets, as a top-level key of its
`vibesys.input.toml`:

```toml
version = 1
backends = ["cpu"]            # or ["cuda"], ["rocm"], ["trainium"], ["metal"]
```

A run takes its backend from the task, never from the CLI's default:

- One backend declared: the run uses it.
- Several declared, in preference order: the run uses the first this host
  provides (`cuda` needs an NVIDIA GPU, `rocm` a `/dev/kfd`, `trainium` Neuron
  devices, `metal` Apple Silicon; `cpu` always). When the host provides none, the first
  is kept and `vibesys preflight` fails naming the task and that backend.
- `--backend NAME` overrides the choice and must be one the task declares, else
  the run is refused before it starts.
- A task that declares none (written before backends were declared) falls back,
  explicitly, to `--backend`, then `[backend].name` in `agent.toml`, whose default
  is `cuda`. That default fails a CPU task on a CPU-only host, so declare
  `backends`. `[resources].accelerator_backend`, when a task declares resources
  for a cluster, counts as its declaration and must be one of `backends` if both
  are given. Every bundled example declares its backends
  (`tests/examples/test_example_registry.py`).

`vibesys preflight` (and the desktop app's Ready to start checks) fail when the
host cannot provide the backend the run resolved to, reporting a typed fact (the
task, the backend and the declared set) that the CLI words with `--backend` and
the app words for its own controls.

## Task Layout Only

Task inputs live only in `.vibesys/tasks/<task>/`. A project with a root
`vibesys.input.toml` fails validation with an error that names the file and
says to move it, with `OBJECTIVE.md` and the files its commands read, to
`.vibesys/tasks/<task>/vibesys.input.toml`. A directory without
`.vibesys/tasks/` is not a VibeSys project.

A repository-shaped example nested below another Git root uses `--runs-dir` to
materialize an isolated project:

```bash
vibesys \
  --runs-dir /work/vibesys-runs \
  --local \
  --project /path/to/example --task default
```

VibeSys provisions a self-contained project below the runs directory, with
`[[workspace.sources]]` checked out. The source's `.git/`, `.vibesys/state/`, `agent.toml`, and
`.env*` files are not copied. Omit `--local` to publish the copied project using
the authenticated GitHub CLI. `vibesys example new` is the simpler way to start
from an example.

## Resume

Inside a project, resume the machine-local current run from the configured
VibeSys state home, then the newest run if no current pointer exists. A run ID
selects a specific run:

```bash
cd /path/to/project
vibesys --resume
vibesys --resume <run-id>
```

With `--runs-dir`, the resume argument selects a copied project in the
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

A run resumes only with a VibeSys whose persisted state has the same version.
A change to that state's shape raises the version and has no migration, so a
run written by an older version is refused with an error naming both versions;
start a new run instead.

For a SkyPilot run, the portable environment choice is recorded, while the
operator profile and local profiles-file path must be supplied on the resuming
host. Nonterminal evaluator identity and replayable results live in the run's
machine-local `skypilot` state namespace. See
[Remote Slurm execution](remote-slurm-execution.md).

## Control a Running Run

A run started without the TUI or the web app has no client to steer it from. `vibesys control`
sends it a control through its control inbox, a directory in the run's machine-local state
(under the VibeSys state home, `~/.vibesys` by default, outside the project), from the project
directory (or `--project PATH`):

```bash
vibesys control <run-id> steer "prefer latency over throughput"
vibesys control <run-id> pause
vibesys control <run-id> resume
vibesys control <run-id> resume --fallback
vibesys control <run-id> resume --when capacity_returns
vibesys control <run-id> stop --mode cancel
```

The command validates the control, writes it, and prints its id. The run applies it at its next
control poll, about once a second, or when it resumes if it is not running. A control the run
cannot apply, such as a pause after a stop, is marked rejected: its reason is written to the
inbox's `rejected/` directory. Each control is applied once, even across a crash or when the
same control also arrives over a client's socket: pass `--control-id ID` to resend the same
control safely. An id, once used, keeps its control: sending a different control under it is
refused.

`vibesys control` must run on the machine that holds the run's state home, and refuses a run
whose machine-local state it cannot find there, naming the state home it looked in (set
`VIBESYS_STATE_HOME` to point elsewhere). Controls still pending stay on that machine: if you
resume the run on another host, send them again there; resending under the same id is safe.

A steer goes to the next agent turn of any role unless it names a target. `--role ID` gives it
to the next turn of one role the run's search declares (role ids are the session role ids in the
run's events, such as `dynamic-planner`; a run keeps the roles it started with, even when it
resumes under code that changed them); `--attempt ID` and `--item ID` give it to the next turn
in that attempt or of that work item. The message reaches at most one matching turn, once. A
role the search does not declare, or an attempt that has ended, is rejected with the reason,
and the message reaches no one. An attempt or item the run does not hold yet is accepted, since
the run may start it before it takes the message; if the run still does not hold it then, or
the target ends before a turn of it runs, the message is reported as dropped in the run's
events. A message to a parked attempt, or to an item with no attempts left, is not rejected
yet: it waits, and is reported as dropped when the run ends (an attempt reopened later is a
new generation, which the message does not reach). For example:

```bash
vibesys control <run-id> steer "re-plan around the new profile" --role dynamic-planner
```

`--interrupt` offers the message to a turn of the target that is running now, on a provider
that takes messages mid-turn. With no such turn running, a provider that cannot take it, or a
turn that refuses it, or a turn that ends before its provider takes it, the next turn of the
target gets it instead. The run's events say which happened: `steer_delivered_mid_turn`, or
`steer_consumed` with a note that no running turn kept it. Delivery is at least once: after a
restart, the next turn may get a message the running turn already had. When the provider took
the message and the run ends before any turn confirms it, the run reports the steer as one
that may have reached the agent, not as one that reached no turn.

```bash
vibesys control <run-id> steer "stop tuning the tile size; profile the copy first" --interrupt
```

A steer reaches one turn. `--standing` makes it standing guidance instead: every turn of its
target dispatched from then on receives it, in a section of its own, until you withdraw it by
the id `vibesys control` printed:

```bash
vibesys control <run-id> --control-id keep-bf16 steer "keep the weights in bf16" --standing
vibesys control <run-id> withdraw keep-bf16
```

A withdrawal is rejected when the run already holds the steer it names and that steer is not
standing guidance, or its guidance has already ended. A withdrawal sent before the run has taken
its steer ends nothing: the run reports that and sets the withdrawal aside, so send it again once
the guidance is in force.
The run's events report once when standing guidance stops reaching turns, with the reason: it
was withdrawn, its target or the run ended, or the attempt it names was reopened (a reopened
attempt is a new generation, which the guidance does not reach). A launch `--constraint` is
not standing guidance: it holds for the whole run and cannot be withdrawn.

## Supply an Input with CLI Flags

Automation can supply the task contract without first writing a project.
This form requires `--runs-dir`. VibeSys synthesizes a one-task project (task
`default`) under `<runs-dir>/_inputs/<exp-name>/`, then provisions it like any
other project.

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

Use this form for CI or programmatic integrations. Checked-in task files
under `.vibesys/tasks/` are easier to review for projects maintained by people.

## Presentation and Validation

- `vibesys` launches the TUI when attached to a terminal.
- `vibesys --headless` disables the TUI.
- Non-interactive execution runs headless automatically.
- `vibesys validate [PROJECT] --task NAME` checks the static task contract
  without starting an agent or executing the checker and benchmark.
- `vibesys tasks [PROJECT] [--json]` lists the tasks a project defines.
- `vibesys example list [--json]` and `vibesys example new EXAMPLE DEST` list the
  example catalog and copy an example into a new project.


See the [CLI reference](cli-flags.md) for every flag and
[`examples/`](https://github.com/uw-syfi/vibesys/tree/main/examples) for complete objectives and manifests.
