# The vibesys-cli task environment

This page says what a task is to `vibesys-cli` (the Go binary under `cli/`),
what the CLI owns when it runs one, and why. The vocabulary is the one in
[Sandboxing and confinement](sandboxing.md): the *run environment* is where a
job's commands execute, and here it is always a container. The CLI's own
module layout is in `cli/README.md`.

## Why

A task used to describe how to reach its machine. The `spark-int4` example
task needed about 300 lines of Python (`run_remote.py`, `on_spark.py`) to copy
a checkout to a DGX Spark over ssh, start `srun`, start and stop the candidate
server, stream output, cancel the Slurm job on a signal and copy the result
stream back. That layer lost a benchmark's result file: the stream was written
relative to the wrong directory, the fetch found nothing, and every sample
failed. None of that code is about what the task measures, and every task that
uses a GPU machine would write it again.

Docker already does most of what the layer did: an image fixes the toolchain, a
container contains the processes, a bind mount carries files, and container
output is the log. The CLI adds what Docker does not know: which machine,
whether it goes through Slurm, GPU access, a build cache that outlives a job,
and a place for a job's artifacts.

## What a task is

A task is three things in `.vibesys/tasks/<task>/`:

1. **One `Dockerfile`: the environment.** Everything installed: the toolchain,
   pinned tools, a reference build, fixed inputs. Its build context is the task
   directory, so it can `COPY` the task's scripts into the image (for example
   to `/task`). The image is built from the task's files, never from the
   candidate's checkout, so a candidate cannot change the checker or the
   benchmark it is judged by.
2. **Scripts, in any language.** They build the candidate, start servers, run
   checkers and load generators. The CLI never looks inside them.
3. **The manifest, `vibesys.input.toml`,** which lists entrypoints and their
   contracts, and nothing about machines:
   - `[accuracy] command`: exit 0 means the commit passes.
   - `[benchmark] command`: writes evaluator records (result protocol 2) to the
     path after `--vs-output`.
   - Each has a `timeout_seconds`.

The manifest has no notion of a server, a step graph or a background process:
those are a script's business, inside one container. The older `[evaluator]`
and `entrypoint` forms belong to the Python evaluator package and stay
refused.

## What the CLI owns

| Concern | How |
| --- | --- |
| Identity of the environment | The image digest, plus the machine: architecture, CPU model and count, and, when GPUs are passed, GPU model, count, driver and the driver's CUDA version. Two results are comparable only when all of these are equal. |
| Containment | One container per command run (a check, or one benchmark sample). The CLI removes it on every path: exit, timeout, cancel, and the death of the process that started it. |
| Placement | Where the Docker engine runs: this machine, another machine over ssh, or another machine inside a Slurm allocation. Chosen in the operator config. |
| GPU access | `docker run --gpus <value>`, or `--runtime nvidia` with `NVIDIA_VISIBLE_DEVICES`. Never CDI. |
| Build cache | A directory per task image on the engine's machine, mounted at `/vibesys/cache` and kept across commits and jobs. |
| Artifacts | A directory per command run, mounted at `/vibesys/artifacts`, kept with the job afterwards. |
| Machine-local inputs | Read-only directories the operator names, mounted at `/vibesys/mounts/<name>`. |
| Results and errors | Result protocol 2 records, including typed error records. |

What the task owns: what checking and benchmarking mean, how the candidate is
built and started, and which inputs it trusts (a script checks the digest of
what it reads from a mount).

## The operator config

Settings about the machine belong to whoever runs the CLI, never to a task: the
host, the Slurm partition and GRES, Slurm options, GPU passthrough and
machine-local mounts. They live in `$XDG_CONFIG_HOME/vibesys/cli.toml`
(`~/.config/vibesys/cli.toml` by default; `VIBESYS_CLI_CONFIG` names another
file). Unknown keys are refused with the key's name. With no file, docker jobs
run on this machine with no GPU, as before. The keys follow `vs-slurm`'s
operator config where they mean the same thing (`host`, `ssh_command`,
`rsync_command`, `remote_workspace_root`, `poll_interval_seconds`,
`queue_timeout_seconds`).

```toml
[docker]
placement = "slurm"        # "local" (default), "ssh" or "slurm"
gpus = "all"               # a docker --gpus value; empty passes no GPU
runtime = ""               # "nvidia": --runtime nvidia instead of --gpus

[docker.mounts]
hf-cache = "/home/user/.cache/huggingface"   # on the engine's machine, read-only

[remote]                   # for "ssh" and "slurm"
host = "host-a"            # an ssh destination or alias
remote_workspace_root = "/home/user/vibesys-cli"

[slurm]
partition = "gpu"
gres = "gpu:1"             # what a benchmark sample asks for
shared_gres = "shard:1"    # what a check asks for; gres when unset
exclusive = true           # a benchmark sample holds the whole node
cpus = 8                   # what a build and a check ask for
memory = "32G"             # optional, a Slurm size
srun_arguments = []
queue_timeout_seconds = 86400
```

## Decisions

### The environment kind stays `docker`; placement is configuration

A job's environment kind is what contains it (`host` or `docker`); it is part
of the job and of every saved fingerprint. Where the engine runs is not a new
kind: local Docker, Docker over ssh and Docker in a Slurm allocation run the
same image the same way, and the fingerprint's machine facts already say which
machine a result came from. Adding `slurm` as a kind would have changed the
contract (`EnvironmentKind` in `environment.proto`) and split results that are
comparable. Each placement is still its own package behind
`environment.Environment`, chosen by name from the operator config, and every
one passes the same environment contract suite (`envtest`).

### The image is built from the task directory and named by the task version

The build context is a tar of the task directory, sent on standard input, so
the build sees the task's files and nothing else; files a tool generates
(`__pycache__`, `*.pyc`) are left out, as they are of the task version. The image tag is the task
version (the digest of every file in the directory), so the image changes
exactly when the task does. The old rule that refused `COPY` and `ADD` is gone:
it existed because the build had no context. The image digest, not the tag,
is what the fingerprint pins and what containers are made from.

### Every sample starts its own container

A benchmark sample is one command run, and one run is one container. Each
sample starts from the same state, the server a script started for an earlier
sample cannot answer for a later one, and the cleanup rule stays one rule. The
cost is a container start (about a second) and, under Slurm, one allocation per
sample: another job may run between two samples, which does not touch a
measurement because each sample holds the whole node while it runs. The
candidate's build is not repeated: it lives in the cache directory.

### Large machine-local inputs are operator mounts

A 26 GB model in the Spark's Hugging Face cache must not be copied into an
image or through ssh. The operator declares it as a read-only mount by name in
`[docker.mounts]`; every container gets it at `/vibesys/mounts/<name>`, and
`VIBESYS_MOUNTS` lists the names. The manifest does not declare it: whether a
machine has the file is a fact of the machine, like its GPU, and the task
already checks the digest of what it reads, so a wrong or missing file fails
the check with the task's own message. Mounts are not part of the fingerprint
for the same reason: the content is verified, the path is not.

### A benchmark without `result_protocol` stays allowed

`[benchmark.result]` (one JSON document and one field) stays: existing tasks use
it, the CLI already reads it, and it adds no concept. A benchmark with neither
it nor `result_protocol = 2` is still refused, and a multi-objective task still
needs protocol 2. The new form is documented and the example task uses
protocol 2.

### Typed errors come back through the record stream

When a benchmark exits non-zero and its stream holds an error record, the job's
reason carries the record's message (and its requirement, when the record says
one was not met), so a failure says why instead of only "exited with code 1".

### The cache directory is keyed by task and image

`/vibesys/cache` is a directory on the engine's machine named by the task and
the image digest. It is shared by every commit and job of the task under that
image, so a candidate's compiler cache (ccache) survives from one commit to the
next, and a new image starts empty. It is never part of a fingerprint and may
be deleted at any time; a task must treat it as a cache. Jobs of one daemon run
one at a time, so nothing else writes it while a command runs.

### Artifacts are kept with the job

`/vibesys/artifacts` is empty when a command starts. After the command, the CLI
copies it to the repository's state directory, one directory per job and per
command run (`01-check`, `02-benchmark`, ...), and the job's output names the
path. A script puts there what a person may want after the job: server logs,
profiles, raw load-generator exports.

### Placement on another machine copies the checkout, never the image

For `ssh` and `slurm`, the CLI copies the job's clean checkout and its scratch
directory to `remote_workspace_root` with `rsync`, mounts them in the container
at their local paths (so a path in a command means the same thing inside), and
copies the scratch directory and the artifacts back when the command ends. A
copy that fails makes the run's ending unknown, not a success. One job's
command runs share its staged checkout, so a candidate built by the check is
still there for the samples; the next job's first run removes the directories
of jobs before it. The Docker engine there is reached by running `docker`
through `ssh`, so the image is built and kept on the machine that runs it.

### Slurm: the allocation holds the container

Under `slurm`, the container's first process is started by `srun` inside a new
allocation (sized as the next section says, with the configured partition and
extra arguments, and a time limit of the command's timeout plus a margin), and
image builds run under `srun` too, so neither runs on CPUs Slurm gave to
another job. The Docker daemon runs outside Slurm's cgroups, so ending the
allocation does not end the container; the CLI removes the container itself on
every path, then cancels the allocation and confirms it ended. From `vs-slurm`
this keeps the semantics, not the code:

- No requeue: a job Slurm requeues would run a command twice. `vs-slurm`
  passes `--no-requeue` to `sbatch`; an allocation `srun` makes is interactive,
  and Slurm requeues batch jobs only, so `srun` has no such option (Slurm
  23.11 refuses it) and none is needed.
- State is read with `squeue`, then `sacct` once `squeue` no longer lists the
  job.
- A cancel is `scancel` by job name, then confirmed by waiting until `squeue`
  no longer lists the job and `sacct` reports it ended.
- A run is found again by its Slurm job name, which carries the daemon's scope
  and the run's id: a daemon restart does not touch a running job (its
  supervisor process owns it), and the run after a supervisor died cancels
  what that supervisor left, by name, before it starts.
- An exit status the CLI did not see, such as an ssh connection that dropped
  (ssh's own exit code, 255), is unknown, never 0.

The queue wait does not count against a command's timeout: the limit starts
when the container runs. The wait is bounded by `queue_timeout_seconds`.

The ssh host must be the node that Docker runs on: the CLI inspects images,
starts processes in the container and removes it by running `docker` there.
That fits a single-node Slurm partition such as the DGX Spark, or a partition
pinned to one node with `srun_arguments`. GPUs are passed with the configured
`--gpus` value, not the GPUs Slurm allocated: on a node with one GPU, such as
the Spark, "all" is the allocated GPU; on a node with several, a check that is
not exclusive could see a GPU allocated to another job (see "What is not
done").

### An allocation asks for what its command needs

Only a timed command needs the node to itself. Holding the whole node for an
image build or a check would make every other job on the node wait hours for
work that does not care what runs beside it, so an allocation is sized by what
it runs:

| Command | Asks for | Confined by |
| --- | --- | --- |
| Image build | `cpus` and `memory`; no GRES, not exclusive | a BuildKit builder of its own whose container is limited to the allocation's CPUs and `memory` |
| Check (accuracy) | `shared_gres` (or `gres`), `cpus` and `memory`; not exclusive | `docker run --cpuset-cpus` with the allocation's CPUs |
| Benchmark sample | `gres` and, with `exclusive = true`, the whole node | the same, which is then every CPU |

The core marks an execution `Measured` when it is a benchmark sample, the docker
environment passes that to the engine, and only the Slurm allocator reads it. A
task cannot ask for an exclusive check: the manifest stays free of machines,
and a check that is slow beside other work only takes longer. The CPU list
comes from the allocation itself (`Cpus_allowed_list` of the srun step), so it
is the set Slurm's cgroup gives the step, not a guess. A program that sizes its
thread pool by the machine's CPUs instead of its affinity oversubscribes that
share: llama.cpp counts `/sys/devices/system/cpu`, and 20 spinning threads on
8 CPUs made the example's CPU reference miss its checker's timeouts. The task
sizes such a pool from `sched_getaffinity` (the example's `step.py` sets
`LLAMA_ARG_THREADS` when it has less than the whole node).

Builds cannot be confined the obvious ways. The Docker daemon runs outside
Slurm's cgroups, so the build's CPU use is not the `srun` step's; BuildKit,
Docker's default builder, takes no `--cpuset-cpus`; and the legacy builder,
which does, has no `RUN --mount=type=cache`, which a task's Dockerfile may use
to keep a compiler cache across image builds. So a build runs in a `buildx`
builder with the `docker-container` driver, created inside the allocation with
`--driver-opt cpuset-cpus=<the step's CPUs>,memory=<memory>`, used with
`--builder <name> --load`, and removed with `buildx rm --keep-state` when the
build ends. Its state (layers and cache mounts) lives in a volume named by the
builder, and the builder is named by the daemon's scope, so the next build
creates a builder of the same name, with that allocation's CPUs, over the same
state: checked by building twice through two builders and seeing the cache
mount's contents survive, and the second builder's container limited to its
own CPU set. A builder a killed build left is removed before the next build
and after a build that did not finish.

### GPU passthrough uses the runtime, not CDI

The Spark's Docker 29.2.1 has the `nvidia` runtime registered and no CDI
devices, so `--gpus all` (through the NVIDIA hook) or `--runtime nvidia` with
`NVIDIA_VISIBLE_DEVICES` work and CDI names do not. A task for the `cuda`
backend is refused unless the operator config passes GPUs; other accelerator
backends are refused.

### One instance per user, and how to run a second one

The daemon's runtime directory comes from `XDG_RUNTIME_DIR`, its task index
from `XDG_STATE_HOME`, and the operator config from `VIBESYS_CLI_CONFIG`, so a
second daemon with its own values of the three is independent of the first:
its containers carry another scope label and its Slurm jobs another name. A
task's store lives in the repository (`.vibesys-state/`) and every linked
worktree of a repository shares it, so a second instance that must not touch a
running one's store works in a separate clone.

## What is not done

- Multi-node Slurm clusters, where the ssh host is a login node: images, exec
  and removal would have to go through `srun --overlap` into the allocation, and
  the machine facts and load a result records, which the environment reads over
  ssh, would have to be read on the allocated node.
- Passing exactly the GPUs Slurm allocated on a node with several, instead of
  the configured `--gpus` value.
- A build's log is kept only when the build fails (its last lines are in the
  error); a successful build's output is discarded, so its duration is not in
  the job's log.
- The cache directory is keyed by the image, so a change to any file of the
  task starts the candidate's compiler cache cold (on the Spark, 308 s for the
  candidate build instead of 9 to 10 s warm). Keying it by the task alone
  would keep it, at the cost of a cache that a new toolchain may misuse.
- Without GPU shards, one GPU is one GRES: dropping `--exclusive` lets a
  check share the node's CPUs, not its GPU, so two GPU jobs on a one-GPU node
  still run one after the other. With the node's GPU split into Slurm shards
  (`Gres=gpu:gb10:1,shard:4`), `shared_gres = "shard:1"` lets up to four checks
  share it, while a sample still asks for `gpu:1` and the whole node. A shard
  is a scheduling token, not a limit on the GPU, and `memory` is one too for a
  container (the Docker daemon runs outside Slurm's cgroups): a bounded
  `memory` keeps Slurm from packing more checks onto the node than its unified
  memory holds, but nothing stops one check from using more.
