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

The manifest declares the task schema it is written in, as a quoted
`MAJOR.MINOR` string: `version = "0.1"` for the single-image format above, or
`version = "0.2"` for a task of two images (below). In 0.1 the manifest has no
notion of a server, a step graph or a background process: those are a script's
business, inside one container. The older `[evaluator]` and `entrypoint` forms
belong to the Python evaluator package and stay refused.

### A task of two images (schema 0.2)

`[candidate]` is optional. A 0.2 manifest without it is a task of one image,
read like a 0.1 command task: the task image runs the check and the benchmark,
which own the system under test (a microservice task, for example, deploys its
app from its own trusted code). It has no candidate image and no server; a
command job runs with `--image harness`, where the check and the benchmark run,
and `--wrap` puts a profiler in front of the command. With
no `Dockerfile` either, its commands run on the host, which the Metal backend
needs: the `Llama-3.1-8B-Instruct-MLX-8bit` example is such a task, whose
`bench.py` starts the candidate's server from the checkout, asks it and stops
its process group in each of its modes. The rest of this section describes a
manifest with `[candidate]`.

Schema 0.2 splits the judge from the candidate. The task's `Dockerfile` still
builds the task image, which runs the check and the benchmark. The candidate's
commit builds a second image, the candidate image, from
a Dockerfile and context it names, and its server runs there:

```toml
version = "0.2"
backends = ["cuda"]

[agent]
domain = "model-serving"

[candidate]
dockerfile = "serve/Dockerfile"      # in the candidate's checkout
context = "serve"                    # its .dockerignore is honored
serve = ["python", "${PROJECT_ROOT}/serve/server.py", "--port", "${PORT}"]
ready = "/health"                    # polled until it answers 2xx
startup_timeout_seconds = 600
allowed_bases = ["vllm/vllm-openai:v0.6.3"]   # optional: the first FROM must be one

[accuracy]
command = ["python", "/task/check.py"]
timeout_seconds = 1800

[benchmark]
command = ["python", "/task/bench.py"]
timeout_seconds = 3600
result_protocol = 2

[environment]
needs = ["gpu", "mounts"]
```

The schema is `TaskManifestV02` in `proto/vibesys/cli/v1/task.proto`, the only
definition of its keys; the CLI decodes the TOML into it and refuses an
unknown key by name. Its keys are every key of 0.1, with the same meaning,
except `[evaluator]` and a command's `entrypoint` and `args`, which it refuses
by name, plus an optional `[candidate]`:

- `version`, `backends`, `metric`;
- `[agent]` `domain` and `interface`;
- `[build]`, `[accuracy]` (or `[check]`) and `[benchmark]` (or `[measure]`),
  each with `command` and `timeout_seconds`; the benchmark also
  has `result_protocol`, `[benchmark.result]` (`json_argument`, `metric`) and
  `requires_accuracy`;
- `[candidate]` `dockerfile`, `context`, `serve`, `ready`,
  `startup_timeout_seconds` and `allowed_bases`;
- `[environment]` `needs`, `docker_in_docker` and `modal` (refused, as in 0.1);
- `[profile_guided]`, `[resources]`, `[workspace]`, `[plan]`, `[catalogue]` and
  `[[comparison]]`, accepted without being read, as in 0.1;
- `[profile]`, retired with the profile job: still accepted with its `command`
  and `timeout_seconds`, so a manifest written for it loads, and not read.

Every command of a job then runs in two containers: the candidate's server
(`serve`, from the checkout's root, with `${PORT}` replaced by the port of the
job) in the candidate image, and the command in the task image, joined to the
server's network namespace, with `VIBESYS_SERVICE_URL=http://127.0.0.1:<port>`.
A command job names the image it runs in. `submit command <commit> --image
harness -- <argv>` runs the command in the task image beside the candidate's
server, exactly as the check and the benchmark run (the same server start,
readiness poll, `VIBESYS_SERVICE_URL` and resource shares), and `--wrap
'<prefix>'` starts the server under the prefix, such as a profiler's command
line; the server's startup time is added to the command's `--timeout`. `submit
command <commit> --image candidate -- <argv>` runs the command in the candidate
image, with no server, and `--wrap` goes in front of the command (or of
`--service`). Either is never cached or scored, and its artifacts are kept with
it. A profile is a harness command: `submit command <sha> --image harness
--wrap '<profiler>' -- <load argv>`.

### A server kept for the job (schema 0.2, `[service]`)

A task of one image whose system under test is a server that takes long to
start (an inference server that loads its weights for most of an hour) can
declare it, so that a measure job starts it once and runs every sample against
it, instead of each sample's script starting and stopping it:

```toml
version = "0.2"

[service]
command = ["python3", "${PROJECT_ROOT}/.vibesys/tasks/t/_server.py", "--port", "${PORT}"]
ready = "/health"                  # polled on 127.0.0.1:${PORT} until it answers 2xx
startup_timeout_seconds = 3600
share_with_accuracy = false        # the default

[service.reset]
command = ["python3", "${PROJECT_ROOT}/.vibesys/tasks/t/reset.py"]
timeout_seconds = 300
```

The schema is `TaskServiceV02` in `task.proto`. The command must name
`${PORT}`; the reset is required. A task with `[candidate]` refuses `[service]`
(its candidate's server is started for each command), and so does a task with
no `Dockerfile`, since the server runs in a container of the task's image.

A measure job runs the check as it would without `[service]`, then starts the
server in a container of the task's image of its own, which gets every resource
of the placement (the GPUs among them) and the whole machine, and waits until
`ready` answers. Each sample then runs in a container of its own beside it,
joined to its network namespace, with `VIBESYS_SERVICE_URL` and without the
GPUs, as the task container of a task of two images runs. Before every sample
that runs on a server an earlier command of the job used, the job asks whether
the server still runs: a server that ended is started again, never reused; one
that runs gets the reset, in a container beside it like a sample's, and a reset
that does not exit 0 fails the sample, and the job with it, naming the sample.
A server that ends while a sample runs fails that sample with the server's last
lines. With `share_with_accuracy = true` the server starts before the check,
which runs against it (in a check job too), and the first sample reuses it.

The server's container is removed when the job ends, however it ends: Close of
the job's shell (`flowrun`) ends it, on a pass, a failure and a cancel alike,
after SIGTERM and the grace period. If the job's supervisor dies instead, the
container stops by itself, as every container of a run does when its caller's
end of its input closes, and the next run of the scope removes it. Under
`apptainer` the server's container holds the allocation (or the claim of a
reused one) for as long as the job may use it, its startup and every command
still to run, and each sample's container joins it: the server is one
`srun --overlap` step of the allocation and every sample another, and the
allocation, or the claim, ends with the job.

Each measurement records, per sample, the server it ran on in
`MeasureResult.sample_services`: `STARTED` (the first command on the server the
job started), `REUSED` (after the reset, on a server an earlier command used)
or `RESTARTED` (the first command on a server started again). The job's output
says when the server starts, is ready, and is started again.

## What a job's commands are told

A command of a job, on the host and in a container alike, finds the job it runs in
in one message, `JobEnvironment` (`proto/vibesys/cli/v1/env.proto`), written as
variables by `cli/envcodec` and read by the evaluator package (a Python reader and a
Go one, each small). One corpus
(`proto/vibesys/cli/v1/conformance/environment.json`) holds all three to the same bytes:

| Field | Variable | Meaning |
| --- | --- | --- |
| `artifacts` | `VIBESYS_ARTIFACTS_DIR` | where the job keeps files that stay with it |
| `cache` | `VIBESYS_CACHE_DIR` | files kept between runs of the same task image (containers) |
| `service_url` | `VIBESYS_SERVICE_URL` | the candidate's server, for a task of two images, or the job's server, for a task with `[service]` |
| `timeout_seconds` | `VIBESYS_JOB_TIMEOUT_SECONDS` | the longest the job may run, in whole seconds, rounded up |
| `mounts` | `VIBESYS_MOUNTS` | the names of the read-only data sets, separated by commas |

A job that needs a cluster also gets `KubernetesEnvironment`: `KUBECONFIG`,
`VIBESYS_K8S_REGISTRY` and `VIBESYS_K8S_BUILDER`. A variable that does not apply
to a job is removed from its environment, even if the daemon's own holds it.
A value that breaks a rule is refused by the reader with an error naming the
variable.

Evaluator packages are versioned and published, and a task image pins one. The
variable names and values are the ones the last release (0.2.0rc1) reads, and the
task directory still holds `vibesys.input.toml`, which that release reads for the
time limit itself, so a job built against it runs unchanged. The new variable is
read only by a newer evaluator package: the time limit that its Kubernetes
deploy sweeps by comes from `VIBESYS_JOB_TIMEOUT_SECONDS` and not from the
manifest, so a newer package run by an older `vibesys-cli`, which does not write
it, fails its deploy with an error saying the variable is not set (and, to run
by hand, to set it to the seconds the job may run). Nothing else in the package
needs it. A package that reads a malformed value of any of the variables above
refuses it naming the variable, where the release took it as it was.

## What the CLI owns

| Concern | How |
| --- | --- |
| Identity of the environment | The image digest, plus the machine: architecture, CPU model and count, and, when GPUs are passed, GPU model, count, driver and the driver's CUDA version. Two results are comparable only when all of these are equal. |
| Containment | One container per command run (a check, or one benchmark sample). The CLI removes it on every path: exit, timeout, cancel, and the death of the process that started it. A timeout or a cancel first sends SIGTERM to the command's processes and waits for them to end, up to `stop_grace_seconds` (30 by default), so a trusted wrapper can delete what it created outside the container; then the container is removed. |
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
host, the Slurm partition and GRES, Slurm options, GPU passthrough, a
Kubernetes cluster and machine-local mounts. They live in
`$XDG_CONFIG_HOME/vibesys/cli.toml` (`~/.config/vibesys/cli.toml` by default;
`VIBESYS_CLI_CONFIG` names another file), whose schema is `OperatorConfig` in
`proto/vibesys/cli/v1/operator.proto`. Unknown keys are refused with the key's
name. With no file, docker jobs run on this machine with no GPU, as before.

A file names placements, where a docker job's container runs, and the
resources each gives a container. A task names the resources its jobs need
(`[environment] needs = ["kubernetes"]`), and a job may run on any placement
that gives every need, which the scheduler chooses when the job starts, or only
on the one `--placement` or `VIBESYS_PLACEMENT` restricts it to. Placements are
interchangeable: where a result was taken is recorded with it and does not
decide which results are compared or reused. The [CLI README](https://github.com/uw-syfi/vibesys/blob/main/cli/README.md#the-operator-config) lists every key.
A file of the earlier shape below is read as the one placement `default`. The keys follow `vs-slurm`'s
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
machine a result came from (for diagnosis; they do not split a docker
environment's results). Adding `slurm` as a kind would have changed the
contract (`EnvironmentKind` in `environment.proto`) and split results that are
comparable. Each placement is still its own package behind
`environment.Environment`, chosen by name from the operator config, and every
one passes the same environment contract suite (`envtest`).

### The image is built from the task directory and named by the task version

The build context is a tar of the task directory, sent on standard input, so
the build sees the task's files and nothing else; files a tool generates
(`__pycache__`, `*.pyc`) are left out, as they are of the task version. The image tag is a digest
of the task version (the digest of every file and symbolic link in the directory) and of the
local image ID of each image the Dockerfile builds on (`FROM`, `COPY --from=`),
so the image changes exactly when the task does or a base changes under its
tag; docker builds on its local image of a tag, so a key of files alone would
keep an image built on a base that was since rebuilt.

A symbolic link in the task directory is a link in the build context, to the
target it holds, so `COPY` puts it in the image as it is in the repository. It
works there only if it leads, link by link, to a file or a directory of the task
directory, since nothing else is in the context. So a task with a `Dockerfile`
is refused when it is read, before any job runs, if one of its links is
absolute, points outside the task directory, points at a path the commit does
not have (a file the task generates, say), or is part of a cycle; the error
names the link and says which. A link to a file a command writes at run time
cannot be checked, so such a task commits the file, makes the link relative to a
committed one, or has the command write the file itself. A task with no
`Dockerfile` runs in the checkout, where git makes every link as the commit
holds it, so its links are not checked; they are still part of its version.
Any other kind of entry (a device, a socket) is refused rather than left out. The old rule that refused `COPY` and `ADD` is gone:
it existed because the build had no context. The image digest, not the tag,
is what the fingerprint pins and what containers are made from.

### Every sample starts its own container

A benchmark sample is one command run, and one run is one container. Each
sample starts from the same state, the server a script started for an earlier
sample cannot answer for a later one, and the cleanup rule stays one rule. The
cost is a container start (about a second) and, under Slurm, one allocation per
sample (under `apptainer`, unless allocations are reused; see below): another job may run between two samples, which does not touch a
measurement because each sample holds the whole node while it runs. The
candidate's build is not repeated: it lives in the cache directory.

A task with `[service]` opts out of this for its server, and only for it: each
sample still runs in a container of its own, while the server outlives them
(next section).

### A job-scoped server is opted into, and each sample says which server it ran on

A server that loads its weights for most of an hour, measured by a benchmark
of a quarter of that, spends most of a measure job starting. Keeping it for
the job trades each sample's fresh start for that time, so it is a choice the
task makes, never a default: a sample on a reused server may differ from one
on a fresh server (a warm cache, a fragmented allocator), which the task's
reset is there to undo, and which the result records so that nobody reads one
for the other.

It is the two-image design with the server's lifetime moved from the command
to the job: the same container topology (`--network container:<server>`), the
same probe (`vibesys-cli probe-ready`, here run in the server's own container),
the same `VIBESYS_SERVICE_URL`, and the same server endings
(`ExecServiceNotReady`, `ExecServiceExited`). The environment gives it as
`Environment.Serve`, which returns a `Service` whose `Run` runs one command
against the server, `Alive` says whether it still runs, and `Close` ends it.
The pure flow decides the lifecycle (`flow.Serve`, `flow.ProbeService`, the
`reset` stage), so restarting a server that ended and resetting one that runs
are decided once, for every environment. The host environment refuses `Serve`,
naming `[service]`, as it refuses a task of two images.

Whether a sample ran on a reused server is recorded per sample, not in the
environment's fingerprint. The fingerprint is the environment's identity, which
the samples of one job share; a job's samples are a mix (the first on a fresh
server, the rest on the reused one), so no one fingerprint describes them.
Adding or removing `[service]` already changes the task's version, which is
part of every result's key and of what `compare` pools, so the samples of the
two modes are never compared with each other.

### Large machine-local inputs are operator mounts

A 26 GB model in the Spark's Hugging Face cache must not be copied into an
image or through ssh. The operator declares it as a read-only mount by name in
the `mounts` resource of a placement (`[placements.<name>.resources.mounts]`,
`[docker.mounts]` in the earlier shape); every container gets it at `/vibesys/mounts/<name>`, and
`VIBESYS_MOUNTS` lists the names. The manifest does not declare it: whether a
machine has the file is a fact of the machine, like its GPU, and the task
already checks the digest of what it reads, so a wrong or missing file fails
the check with the task's own message. A mount's directory is not part of the
fingerprint for the same reason: the content is verified, the path is not. The
names are, since they are what a task reads.

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
backend is refused unless the operator config passes GPUs; a task for the
`rocm` backend unless it gives a `rocm` resource (below); other accelerator
backends are refused.

### AMD GPUs are a resource of their own, checked in the container

`rocm` is a resource kind beside `gpu`, not a vendor setting of it: what the
container gets differs (`--device /dev/kfd --device /dev/dri --group-add
video` under Docker, `--rocm` under Apptainer, and no `--gpus`), its facts
differ (`amd_gpu_model`, `amd_gpu_count` and `amdgpu_driver`, read with
`rocminfo`, or `amd-smi` when it lists none, and `/sys/module/amdgpu/version`),
and a task needs one or the other by its backend. A variable in which Slurm
names the GPUs it assigned (`ROCR_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES`,
`GPU_DEVICE_ORDINAL`, `CUDA_VISIBLE_DEVICES`) is passed on when it is set and
not required, since a partition that does not isolate its nodes' GPUs sets
none.

A ROCm image runs on the node's kernel driver, which may be older or newer than
the image's ROCm supports, and then the runtime sees no GPU and the command
fails in a way that blames the task. So the container first runs a check, a
POSIX shell script, before the command: it asks `rocminfo` (or `amd-smi`) for a
GPU agent and, when there is none, fails the job before its command with
`the container's ROCm runtime does not see the GPU: … (the image's ROCm X, the
node's amdgpu driver Y)`. In a task of two images the GPUs and the check are the
server's.

### Apptainer: a multi-node cluster without Docker

A cluster whose compute nodes run Apptainer and no Docker, reached through a
login node, is the placement kind `apptainer`, not a runtime setting of the
`slurm` kind: the engine, the sections and the lifecycle all differ, and the
`slurm` kind's configs stay as they were. It is a `docker.Engine`, so the docker
environment's decisions (images, the two containers of a task of two images,
the resources, the stop) hold unchanged; only how a container exists differs.

- The image is built by the caller's own Docker, and converted to a SIF on
  the caller's machine by its own `apptainer`, which the placement requires
  on the daemon's `PATH`; the SIF is copied over ssh to the shared file
  system, named by its digest, once, and later runs and other daemons copy
  nothing. The cluster's apptainer only runs SIFs. Converting where the image
  already is was chosen over converting on the cluster after measuring both
  on a 47 GB ROCm image: a node's scratch space (25 GB) cannot hold the image
  unpacked; an unpacked sandbox built on the shared file system (WekaFS)
  wrote 121k of its several hundred thousand files in 30 minutes, an hour or
  two per image; and `mksquashfs` stalls there. Locally, `docker save` took
  202 s and `apptainer build` 475 s, to an 11 GB SIF, which is the one
  transfer. Building on the cluster from the Dockerfile was not an option
  either: its nodes have no Docker, and `apptainer build` of a Dockerfile
  would make another image than the one the result records, so Dockerfiles
  stay the only image recipe.
- `docker save` streams into the conversion and the SIF streams to the
  cluster, so no archive is copied anywhere: the archive is written once, in
  the conversion's own directory under the operator's `local_scratch_dir`,
  beside Apptainer's `APPTAINER_TMPDIR` and `APPTAINER_CACHEDIR`, and the
  directory is removed when the conversion ends. That file system is checked
  first for the image's unpacked size (`docker image ls`; with Docker's
  containerd image store `docker image inspect` reports the compressed
  layers) and twice its stored size (the archive and the SIF), and too
  little fails naming the space needed and the space there is.
- Publishing is a write to a file of the publisher's own and a rename once its
  size is what was sent, so concurrent publishers each rename a whole file,
  and a copy cut short, or a conversion that fails, is never there. What
  converted a SIF, with its version, goes to the build's log and to
  `<digest>.sif.built-by` beside it.
- One batch allocation per run holds both containers, which share the node's
  network; the server's port is chosen free in the allocation, from the node's
  `/proc/net/tcp`, not from the run's id, since other jobs share the node's
  network. The allocation is `sbatch --no-requeue --wrap 'exec sleep infinity'`
  through the operator's submit command, so a site's wrapper and the options it
  requires are configuration. Its processes are `srun --jobid --overlap` steps
  started from the login node. A holder step reads its standard input and
  cancels the allocation when it ends, which is when the run closes it or the
  daemon dies and its ssh connection with it; the run also cancels by name and
  confirms with `squeue` then `sacct`, and a run sweeps its scope's leftovers
  by name. The jobs of one placement run at once, so each job's runs are a
  scope of their own under the placement's, which the allocation's comment
  and the staged directories name, and a job's supervisor holds a lock of its
  run scope while it lives (`runtimedir.Runs`): a run sweeps what its own
  job's earlier attempts and every dead job of the placement left, and never
  a live job's. The holder alone would not do: an allocation still queued
  when its job died has no holder, and would hold a node until its limit.
- A container's processes carry a variable naming it, set inside the container
  only, by which SIGTERM and SIGKILL reach every one of them; the step itself is
  not signalled, so the grace works as under Docker.
- Machine facts, load and GPU facts are read on the node inside the allocation.
  The daemon fingerprints before a job has a node, so it reports the last node
  its scope read, kept on the shared file system, which assumes the partition's
  nodes are alike; the facts are recorded and compared by nothing.
- A step whose command never started (srun or apptainer failed first, which
  the wrapper's start marker on standard error tells) or whose ssh exit is 255
  is unknown, never the command's exit.

### Reused allocations under apptainer

"Every sample starts its own container" holds, but under `apptainer` one
allocation per container made a measure of five samples wait in the queue six
times, which on a contended cluster (hours per wait, one running job per user)
made a job take days. With `idle_release_seconds` an allocation is kept for
the next container instead. It is off by default: a reused allocation always
asks for the whole node, which a check alone does not need, and holds it
while idle, which other users of the partition pay for.

- The pool is on the cluster, not in a process. A run's containers live in
  its supervisor process, which ends with the job, so a pool in the engine
  would have lasted one job. The allocations are found by name and scope with
  `squeue` (with their time left), claimed by a step on the node that records
  its own process under a lock (`flock` in `/tmp/vibesys-pool-<job>`), and kept
  alive by the allocation's own batch script, which ends the allocation once no
  claim has lived for the idle time. So a daemon's death needs no one to cancel
  anything: its claim's step dies with its ssh connection, and the allocation
  ends itself, or is claimed by the next daemon of the scope first.
- One claim at a time is the arbitration: no container runs in a claimed
  allocation but the claimer's own (and the one that joins it in a task of two
  images), so a measured sample still holds the whole node while it runs.
  Nothing is leased between daemons for it, as before. An `apptainer`
  placement may run `max_jobs` jobs at once: the reused allocations are the
  placement's, and a job that finds none free asks for a new one.
- A claim kills every process of a container left in the allocation (one
  that carries `VIBESYS_BOX` and is in the job's cgroup, or carries the job's
  id) and waits for none to be left before it is held; a release does the same
  before the claim is dropped, and an unconfirmed release cancels the
  allocation. So nothing a run left runs beside the next, which is what the
  sweep of a run's scope ensured before; the sweep now leaves the reused
  allocations alone, since cancelling them would undo the reuse.
- A container claims only an allocation with its limit and the stop's margin
  left, the one with the least time left first.
- Sites cap a job's time (one QOS refuses anything over 12 hours), and an
  `sbatch` refusal deep in a job is a poor error. `[slurm] max_time_seconds`
  states the cap, and the engine keeps every allocation under it: a reused
  one's limit defaults to it and may not exceed it, and a container whose limit
  and stop margin do not fit is refused before anything is submitted, naming
  the key. A container with no limit (the environment gives every container
  the run's limit, the server of a task of two images too; only a direct
  caller of the engine gives none) no longer counts as a day: it claims the
  allocation with the most time left, or asks for the reused allocations'
  limit, and runs as long as that lasts. The key is refused on the `slurm`
  kind, whose `srun` jobs it does not cap yet.

### Pyxis and Enroot: a runtime of the apptainer kind, not a kind of its own

A cluster whose nodes run containers with Slurm's Pyxis plugin over Enroot,
and have no Apptainer, is the `apptainer` kind with `runtime = "pyxis"`, not a
placement kind of its own: the allocation, the holder, the steps, the signals,
the facts read on the node, the staging and the image's naming by content are
the same, and only what an image becomes and how a process enters it differ.
A new kind would have duplicated the engine and the operator's sections for
two functions' worth of difference.

- The image becomes an Enroot squashfs, made on the login node from the image's
  files as a container has them (`docker export`), streamed over ssh into
  `sqfstar`, so the image is never unpacked on a disk at either end and only
  the files cross the network once. The other ways were worse: Enroot's own
  `enroot import dockerd://` needs Enroot on the caller's machine, which a
  Docker host does not have; `docker save` into the login node's Podman and
  `enroot import podman://` stores every layer again in Podman's storage, on a
  home file system with a quota, and unpacks the image a second time; building
  the squashfs here needs squashfs-tools here. The stream is rewritten on the
  way as Enroot's own import would: the image's environment is written to
  `/etc/environment` (which Enroot reads, and which is the only part of the
  image's config a container here uses, since the engine runs its own command
  and never the entrypoint), the image's volumes are not mounted (an empty
  `/etc/fstab`), and Docker's `/.dockerenv` and devices are left out. `sqfstar`
  accepts a stream cut short between two entries as a whole one, so the stream
  ends with `/.vibesys-image`, naming the image's content, and the squashfs is
  renamed into place only when it holds that file.
- Each process is a step `srun --container-image=<squashfs>`, read-only and
  unnamed. A named container (`--container-name`) would let a container's
  writes outside its mounts persist from one process to the next, as under
  Docker, but it outlives its step and must be removed with the container,
  and the `apptainer` kind already promises read-only image files; an unnamed
  one is mounted from the squashfs for the step (the site's Enroot mounts
  squashfs images rather than unpacking them) and gone with it. The
  container's home is a directory of the node's mounted at `/tmp`, made by a
  step of its own first, since Pyxis mounts only what exists, so every process
  of the container shares one `/tmp`, as under Apptainer.
- Pyxis gives a container the step's environment beneath the image's own, so
  the step starts from `env -i` with only `PATH` and the container's variables,
  which `--container-env` puts above the image's. The site's Enroot
  configuration decides what else every container gets: on the site this was
  checked on, the node's `/dev` (with the GPUs), `/proc`, `/sys`, a private
  `/tmp` beneath the home, and a few files of `/etc`, and none of the shared
  file system, so no site bind path leaks in. Slurm's variables naming the GPUs
  it assigned are in the step's environment, which Pyxis passes in.

### One daemon per user per machine, and a sandbox beside it

One daemon serves a user on a machine: it holds a lock in the user's machine
directory (`/var/tmp/vibesys-cli-<uid>`, or `/run/user/<uid>` when `/var/tmp`
is not on local disk), which no variable moves, and a daemon of another
runtime directory is refused, naming it. Two daemons with their own
runtime directories would otherwise each schedule as if they had the machine.
A second one on purpose is a sandbox daemon: `VIBESYS_CLI_SANDBOX=1` with its
own `XDG_RUNTIME_DIR`, and its own `XDG_STATE_HOME` and `VIBESYS_CLI_CONFIG`
when it should not share the index or the operator config. The first start in a
runtime directory records its state home there, and later starts reuse it. Its containers
carry another scope label and its Slurm jobs another name, and the daemon's
status lists it while it runs. A task's store lives in the repository
(`.vibesys-state/`) and every linked worktree of a repository shares it, so a
second instance that must not touch a running one's store works in a separate
clone.

### Two images, two containers, one network

The judge must not be the candidate's to change, and the candidate's server
may need a stack the judge does not (a serving framework, a CUDA build). One
image could not give both: either the candidate edits the image that judges it,
or the judge's image carries every candidate's stack. So the task image is
built from the task directory, as before, and the candidate image from the
candidate's commit, never from a working tree. It is named
`vibesys-candidate:<task>-<digest>`, the digest being that of its Dockerfile
and the files of its context that its `.dockerignore` keeps, and is built only
when the engine lacks it. A failed build fails the job before anything runs,
with the end of the build's output. The image's id is recorded with each result
(`Result.candidate_image`) and is not part of the result's key: the commit
already names the candidate, and a rebuild of the same commit that makes
another image (a base that moved) answers from the saved result.

The two containers share a network namespace (`--network container:<server>`),
so the server is at `127.0.0.1` and nothing listens beyond the job. The port is
taken from the job's run id. The server's readiness is polled from the task's
container by the CLI's own binary, mounted read-only at
`/vibesys/bin/vibesys-cli` and run as `vibesys-cli probe-ready <url>`: the task
image needs no HTTP client, and only a static build can run whatever the image
holds, so a placement of a build with cgo (a developer's `go build`) or of
another system refuses tasks of two images, naming why. A server that is not
ready within `startup_timeout_seconds`, or that exits, fails the step with the
last lines of the server's output. Both containers are stopped on every path,
the command's first: SIGTERM, the grace period, then removal.

The placement's resources are shared by what each container is for, from
`[environment] needs`:

| Resource | Candidate's container | Task's container |
| --- | --- | --- |
| `gpu` | yes, with the variables that pass the GPUs | no |
| `mounts` | yes, read-only (model weights) | yes, read-only (tokenizer files) |
| `kubernetes` | no | yes |

A resource that sets a network mode (a `kubernetes` cluster reached over the
host's network) cannot be given to either container, since the task's joins
the server's network; such a placement refuses tasks of two images, naming the
mode. The decisions live in the pure flow (`flow.BuildCandidate`, then each
`flow.Run` with its candidate) and in the docker environment's per-container
specs; a Slurm or Apptainer placement implements the same `Environment`
(`Candidate`, and a run of two containers in one allocation that share a
network namespace) without a change to the flow.

### Task schema versions are MAJOR.MINOR, and 0.x may change freely

A manifest declares its schema as a string, `version = "0.2"`. The integer
`version = 1` of the earliest tasks is refused, saying to write `"0.1"`: a
number would read as a float or an int depending on how it was written. While
the major version is 0, every minor version is a format of its own: the CLI
reads exactly the versions it has (`vibesys-cli --version` lists them),
refuses a newer one saying to run `vibesys-cli upgrade`, and refuses a
malformed one (`"2"`, `"2.x"`, `"02.0"`). From 1.0 a minor version only adds
optional keys: a reader of M.N reads M.0 to M.N, and a file that declares M.K
may set only the keys of M.0 to M.K (each field carries
`task_schema_since_minor`). A major version is a new message beside the old.

`./scripts/check_task_schema.sh`, in the proto check group beside
`buf breaking`, compares the schemas with the merge base: a 0.x schema may change
freely (pre-1.0 formats have no compatibility rules, and `buf breaking` is the
one gate on them); from 1.0, every field declares its minor version, a field added
since the base declares the current minor version, the minor version must rise
when fields are added, and a changed protovalidate rule fails ("tightening
needs a new major; loosening needs a reviewed waiver"). `buf breaking` already
refuses a renamed or removed field.

`objectives.toml` is the message `TaskObjectives` (with `TaskObjective` and
`TaskPareto`), read by `tomlproto` for tasks of every schema: an unknown key is
refused naming the file and the key, and the rules on the message hold. It has
no schema version of its own.

Schema 0.1 has no message in `task.proto` yet: its reader is the earlier Go
reader, unchanged. Of 0.1 the example tasks and the Python input synthesis still
write it, so it is not removed. Moving it into a `TaskManifestV01` message is left for
later; until then the check covers 1.0 and every later schema.

## What is not done

- A job-scoped server (`[service]`) on the host, and on `ssh` and `slurm`
  placements, which have no probe program: they refuse such tasks. A task of
  two images cannot keep its candidate's server for the job.
- Tasks of two images on `ssh` and `slurm` placements: they have no probe
  program and refuse such tasks. The `apptainer` placement has one (the binary
  is copied to the cluster) and runs both containers in one allocation.
- Schema 0.1 as a proto message (above).

- Multi-node Slurm clusters with Docker on the compute nodes, where the ssh
  host is a login node: the `slurm` kind still needs the ssh host to be the
  node Docker runs on. Such a cluster without Docker is the `apptainer` kind,
  which goes through the login node with `srun --overlap` and reads the
  machine on the allocated node.
- Under `apptainer`, the image's own files are read-only (no writable overlay),
  where a Docker container's are not; a task that writes outside its mounts,
  `/tmp` and its home fails there. The site's bind paths are not mounted
  (`--no-mount bind-paths`): a probe found the shared file system bound
  read-write into every container by them, through which a container wrote
  into a shared image; only the node's `/etc/hosts` is bound back. A machine
  that cannot run Apptainer, such as macOS, has no `apptainer` placement.
  The node's GPUs are passed with `--rocm`,
  and whether a step on a partition that isolates GPUs gets the job's GPUs
  without `--gres` on the step is not verified.
- Passing exactly the GPUs Slurm allocated on a node with several, instead of
  the configured `--gpus` value, under `slurm`.
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
