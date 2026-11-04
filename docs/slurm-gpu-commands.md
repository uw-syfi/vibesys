# GPU commands through Slurm

The agent can run its own GPU commands as Slurm jobs. It runs in a local Docker
container on the Slurm submit host and holds no GPUs while it reads, edits, or
thinks. Each GPU command gets its own job, sized and timed for that command.

This is an optional capability of `--run-environment slurm`: add a
`[vibesys.agent_gpu]` table to the operator file (see
[Operator configuration](#operator-configuration)). It needs a host that is also
a Slurm submit node with a shared view of the project, so the file must set
`[slurm.transport] kind = "local"`; any other transport is rejected with an
error naming `vibesys.agent_gpu`. For remote clusters, use `--run-environment
skypilot` (see [Remote Slurm execution](remote-slurm-execution.md)).

`--run-environment slurm-gpu` is a deprecated alias; see
[The deprecated slurm-gpu alias](#the-deprecated-slurm-gpu-alias).

## Agent view

The agent container binds no GPU device nodes and sets `CUDA_VISIBLE_DEVICES=""`.
To use GPUs, the agent runs:

```bash
vibesys-gpu --gpus N --time MINUTES -- COMMAND...
```

The launcher's path is also in `$VIBESYS_GPU`. It applies to tests,
benchmarks, profilers (`ncu`, `nsys`), and any other CUDA process. Output
streams back as the job produces it. The exit status is the command's.
Interrupting the client, or losing its connection, cancels the job, whether it
is queued or running.

## The host bridge

The container cannot reach the Slurm controller, and Docker is not available on
compute nodes. A host-owned broker therefore sits between them:

- The workspace is mounted into the container at the same absolute path it has
  on the host, so a working directory means the same thing on both sides.
- The broker listens on a Unix socket that is bind-mounted into the container,
  and the container receives a per-run token in its environment. The launcher
  is a single Python file that uses only the standard library, so it runs in any
  image that has `python3`.
- The broker checks that the working directory is inside the workspace of the
  session it serves, checks the request against the operator limits (rejecting
  rather than clamping), and runs the command with `srun`.
- A GPU job runs on a compute node under the host sandbox (bubblewrap on Linux,
  Seatbelt on macOS), confined to the workspace. The job environment is the
  host's baseline allowlist plus the variables the agent passed, minus the
  container's identity, its device variables and any `SLURM_*` variable. The
  job sees the workspace (writable), the host's system directories, and the
  paths named by `read_only_paths` (read-only, never writable); nothing else,
  so an interpreter or package in the operator's home directory is invisible
  unless it is listed (see [Operator configuration](#operator-configuration)). The
  submit host checks that it can confine a job, but the job names `bwrap` by
  program, so the node's own `PATH` finds it: install bubblewrap on the compute
  nodes, not only on the submit host.

### Parallel candidates

The dynamic loop evaluates several candidates at once, each in its own git
worktree and its own container session. Every session opens its own broker, with
its own socket and token, and that broker accepts only its own candidate's
worktree. An agent in candidate A that asks for a GPU command in candidate B's
worktree is refused with `commands must run from inside the run's workspace`, so
one candidate cannot read or write another's files through a GPU job. Candidate
sessions run concurrently; Slurm queues their jobs against the operator limits.

`vibesys-gpu -- COMMAND` runs the agent's own GPU commands. It is not an evaluation and
produces no evidence. Every loop's agents have the evaluation tool (`submit_evaluation`,
then `validate_evaluation_wait`): only it scores a candidate, and only its results count.
Numbers the agent measures through `vibesys-gpu` are for its own iteration. The broker
offers no evaluation operation, so `vibesys-gpu --gate` is not a mode of the client, and
the prompts name no gate. The container gets the client only when the capability is on.
The framework's own trusted accuracy and benchmark run on the `sbatch` path through the
core evaluation executor whether or not the agent may run GPU commands, sized by
`sbatch_arguments` and the task's resources.

## Operator configuration

The configuration is machine-local. For `--run-environment slurm` the table is
`[vibesys.agent_gpu]` in `~/.config/vibesys/slurm.toml`, beside `[slurm]`:

```toml
[slurm]
name = "local-cluster"
remote_workspace_root = "/shared/vibesys"   # shared with the compute nodes

[slurm.transport]
kind = "local"
# How this host reaches Slurm. The agent's srun and scancel, and the trusted
# evaluation's sbatch, all run through it. A login-container wrapper may precede the shell.
shell_command = ["bash", "-c"]

[vibesys.agent_gpu]
# Preference order. A partition is used only if its time limit (read from
# `sinfo`) fits the request. With windows_command set, the first such partition
# whose window starts the job now is used; otherwise the first such partition.
partitions = ["main", "priority"]
max_gpus = 8
max_time_minutes = 120
default_time_minutes = 30   # when the agent omits --time
windows_command = ["slurm-windows", "--json"]
srun_arguments = []         # extra site arguments, for example ["--account=lab"]
# Host paths the agent's GPU jobs may read besides the workspace and the system
# directories, for example the Python environment with torch. Read-only always.
read_only_paths = ["/home/operator/venvs/torch"]
# At most this many of the run's GPU jobs, across all of its agents, are submitted
# to Slurm at once. Omit it for no cap.
max_concurrent_jobs = 2
# Seconds between the queue checks that show a stopped or failed command's job is
# gone. Omit it for 5.
queue_check_interval_seconds = 5
```

`max_concurrent_jobs` keeps one run from monopolizing a shared partition: without
it, several agents can keep jobs queued or running back to back, and on a shared
node that steady stream can keep other users' whole-node jobs from ever starting.
A command past the cap waits on the submit host, first come, first served, and is
submitted when one of the run's jobs ends. While it waits, the agent sees one line
such as `[vibesys-gpu] waiting: 2 of this run's GPU jobs are active (cap 2), 1
queued ahead; ...`. Its `--time` limit starts when it is submitted, not while it
waits, and an agent that stops the command while it waits leaves the line without
a job ever being submitted. The value must be a positive integer.

When a command ends with a nonzero status or is stopped, its status is returned
only after the job has been absent from the queue for five checks in a row,
`queue_check_interval_seconds` apart (25 s by default). The window keeps a job
whose submission was acknowledged late from being taken for gone. A smaller
interval shortens the window and the wait; the value must be a positive number.

`read_only_paths` entries must be absolute, normalized paths (no `..`, `.`,
repeated or trailing slashes, and not `/`), must exist on the submit host when
the run starts, and must be visible at the same path on the compute nodes. A
symlink inside an exposed path is followed only if its target is also exposed,
and a job exposes nothing the operator did not list, so the run refuses to start
when a link would dangle. For every symlink directly inside a `bin` or `sbin`
directory of a listed path (the entry points of a venv or install), the check
follows the whole chain the way the job's kernel would. If a hop leaves the
listed paths and the system directories, or the link is broken or loops, the
open fails with a message like:

```text
vibesys.agent_gpu.read_only_paths lists paths holding symlinks that a confined job could not follow, so the job would fail with 'No such file':
  /home/op/venv: /home/op/venv/bin/python -> /home/op/.local/share/uv/python/cpython-3.12-linux/bin/python3.12 reaches /home/op/.local/share/uv/python/cpython-3.12-linux/bin/python3.12, which the job cannot see; add /home/op/.local/share/uv/python/cpython-3.12-linux/bin/python3.12 (or the directory that holds it) to read_only_paths
```

Add the path it names (normally the Python installation directory, which is
what a venv's `python` links to). On Linux, a listed path that is itself a
symlink (uv's `cpython-3.12-...` alias directory) is exposed as the directory it
points to, so listing the alias is enough and the real install need not be
listed; on macOS the resolved path must be listed too. Symlinks outside `bin` and
`sbin` are not checked. The same list applies to the
Linux (bubblewrap) and macOS (Seatbelt) confinement. The agent's prompt says
that GPU commands run in the host's environment, not the container's, and
names these paths.

Unknown keys and out-of-range limits are rejected at launch, naming the key.
`gate_gpus` and `gate_time_minutes` are not accepted here: trusted evaluation is sized by
`[slurm] sbatch_arguments`. `srun_command` and `scancel_command` are not accepted
either: the Slurm binaries are reached through `[slurm.transport] shell_command`,
the one place that says how. The file is parsed once when the run starts, and the
run keeps its own rendering of it in `logs/slurm-operator.toml`.

Every job carries `--partition`, `--gres=gpu:N`, `--time`, and a
`vibesys-gpu-*` job name. A misconfigured or missing `windows_command` only
drops the "starts now" preference: the first partition whose `sinfo` time limit
fits is used. A request that no configured partition's limit fits (or whose
limits `sinfo` did not report) is rejected before `srun`, with the reason on the
job's output and exit status 2.

## Profiling

With the capability on, the agent profiles through `vibesys-gpu` (`nsys` by
default; `ncu` or `rocprof` as the backend dictates), and the run does not
plan a remote ROCprof capture. Without it, the `slurm` environment keeps its
remote ROCprof capture over the SSH transport. With the local transport and no
agent GPU commands there is no transport for the capture, so the run offers no
profiler (`--profiler none` is the only choice).

The `profile-guided` loop's framework-owned `[profile_guided].command` uses the
same attribution route. With local transport and `[vibesys.agent_gpu]`, it runs
as a confined one-GPU Slurm job in the isolated candidate workspace, through
the same run-scoped launcher, partition policy, job-name prefix, concurrency
cap, host environment filter, and `read_only_paths` as `vibesys-gpu`. The
configured seconds timeout remains exact: the scheduler allocation is rounded
up to minutes, and VibeSys cancels it at the requested second. Without the
capability, attribution falls back to the editor command runner. Local
validation remains in the editor in both cases.

Before submission, VibeSys durably records the capture's stable Slurm job name.
Cancellation waits for the job to settle before removing the runtime-owned output
file or discarding the candidate. If the launcher cannot prove the named job left
the queue, the capture fails as unresolved and those resources remain retained
across host cleanup and restart. A resumed host cancels and queries the recorded
name, and sweeps the candidate only after the scheduler confirms the job is absent.

## Launch

```bash
vibesys --project /path/to/project --task TASK \
  --run-environment slurm --slurm-config ~/.config/vibesys/slurm.toml
```

On resume, the recorded environment is restored. Pass `--slurm-config` again
when the configuration is not at the default path.

## The deprecated slurm-gpu alias

`--run-environment slurm-gpu` still works. It reads the old `[slurm_gpu]` table
(default `~/.config/vibesys/slurm-gpu.toml`, or `--slurm-config`), translates it
once to the settings above, reports one deprecation warning when the run starts (not once per environment open), and opens the `slurm`
environment. A run recorded as `slurm-gpu` resumes the same way. The
translation is a pure function; a key it cannot carry over is rejected by name.

| Old key | Becomes |
|---|---|
| `partitions`, `max_gpus`, `max_time_minutes`, `default_time_minutes`, `windows_command`, `job_name_prefix`, `srun_arguments` | the same keys of `[vibesys.agent_gpu]` (`read_only_paths` too); `srun_arguments` reach the agent's jobs only, never the gates (`--pty` is invalid for `sbatch`) |
| `srun_command`, `scancel_command` | `[slurm.transport] shell_command` only: whatever precedes `srun` (a login-container wrapper) becomes the prefix of `shell_command`, through which `srun`, `scancel` and `sbatch` all run. Both commands must end in `srun` / `scancel` and carry the same wrapper, or the file is rejected |
| `gate_gpus`, or the task's `[resources]` accelerators per node | gate `--gres=gpu:N`; a task needing more than `max_gpus` is rejected |
| `gate_time_minutes` | gate `--time=M`; the job timeout is that plus a one hour queue allowance |
| `partitions` (for gates) | gate `--partition=A,B,...`: the scheduler picks the partition that starts earliest, not the first in order |
| (none) | `transport.kind = "local"`, `remote_workspace_root = ~/.cache/vibesys/slurm-gpu-stage` |

Two behaviors differ from the old environment, because the gates now use the
`sbatch` stack: they stage a copy of the workspace into
`~/.cache/vibesys/slurm-gpu-stage` (the old gates ran in the workspace itself),
so that directory must be shared with the compute nodes; and they run the
evaluator through the configured remote Python (`python3`). To change either,
move to a `slurm.toml` as above. A site flag the gates need (such as
`--account=lab`) is likewise set there, as `sbatch_arguments`. Concurrent runs
share the stage directory safely: each gate stages into its own
`slurm-gpu/<invocation id>` subdirectory and gets a unique scheduler job name.

## Limits

- The agent's prompt explains the launcher, but the agent chooses the GPU
  count and time limit. Keep `max_gpus` and `max_time_minutes` at what one
  command may hold.
- Jobs must run on a node that shares the workspace path with the submit host.
- Docker must be usable on the submit host; the agent never runs outside it.
- Gates run on a single node.
- A gate's `timeout_seconds` in the task manifest includes the time its job
  waits in the Slurm queue. On a busy cluster, raise it to cover the expected
  wait; `gate_time_minutes` still bounds how long the job holds GPUs.
