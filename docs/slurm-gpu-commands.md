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

`--run-environment slurm-gpu` is the older spelling of the same capability with
its own `[slurm_gpu]` table, which also sizes the gates (`srun` jobs). It runs
the same launcher, broker, confinement, and limits code.

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
- The broker checks that the working directory is inside the run's workspace
  or one of its candidate worktrees, checks the request against the operator
  limits (rejecting rather than clamping), and runs the command with `srun`.
- A GPU job runs on a compute node under the host sandbox (bubblewrap on Linux,
  Seatbelt on macOS), confined to the workspace. The job environment is the
  host's baseline allowlist plus the variables the agent passed, minus the
  container's identity, its device variables and any `SLURM_*` variable.

The trusted accuracy and benchmark gates are host commands that the framework
planned. The agent runs them as `vibesys-gpu --gate accuracy` or
`vibesys-gpu --gate benchmark`. In the `slurm` environment the gates stay on the
`sbatch` path (`vs_sandbox.slurm_command`) whether or not the agent may run GPU
commands, and are sized by the sbatch arguments and the task's resources. In
`slurm-gpu` the broker runs the planned command unconfined through the same
`srun` launcher; each uses the task's `[resources]` accelerator
count, or `gate_gpus` when the task declares none, and the time limit
`gate_time_minutes`. A benchmark's result file under `/tmp` is written on the
host, so the broker relays it back into the container.

## Operator configuration

The configuration is machine-local. For `--run-environment slurm` the table is
`[vibesys.agent_gpu]` in `~/.config/vibesys/slurm.toml`, beside `[slurm]`:

```toml
[slurm]
name = "local-cluster"
remote_workspace_root = "/shared/vibesys"   # shared with the compute nodes

[slurm.transport]
kind = "local"

[vibesys.agent_gpu]
# Preference order. With windows_command set, the first partition whose window
# starts the job now is used; otherwise the first whose time limit fits.
partitions = ["main", "priority"]
max_gpus = 8
max_time_minutes = 120
default_time_minutes = 30   # when the agent omits --time
windows_command = ["slurm-windows", "--json"]
srun_arguments = []         # extra site arguments, for example ["--account=lab"]
```

Unknown keys and out-of-range limits are rejected at launch, naming the key.
`gate_gpus` and `gate_time_minutes` are not accepted here.

For `--run-environment slurm-gpu` the table is `[slurm_gpu]` in
`~/.config/vibesys/slurm-gpu.toml`; `--slurm-config PATH` overrides it. It takes
the keys above and the two gate keys:

```toml
[slurm_gpu]
# Preference order. With windows_command set, the first partition whose window
# starts the job now is used; otherwise the first whose time limit fits.
partitions = ["main", "priority"]
max_gpus = 8
max_time_minutes = 120
default_time_minutes = 30   # when the agent omits --time
gate_gpus = 1               # when the task declares no [resources]
gate_time_minutes = 60
windows_command = ["slurm-windows", "--json"]
srun_arguments = []         # extra site arguments, for example ["--account=lab"]
```

Every job carries `--partition`, `--gres=gpu:N`, `--time`, and a
`vibesys-gpu-*` job name. A misconfigured or missing `windows_command` falls
back to the first configured partition.

## Profiling

With the capability on, the agent profiles through `vibesys-gpu` (`nsys` by
default; `ncu` or `rocprof` as the backend dictates), and the run does not
plan a remote ROCprof capture. Without it, the `slurm` environment keeps its
remote ROCprof capture.

## Launch

```bash
vibesys --project /path/to/project --task TASK \
  --run-environment slurm-gpu --slurm-config ~/.config/vibesys/slurm-gpu.toml
```

On resume, the recorded environment is restored. Pass `--slurm-config` again
when the configuration is not at the default path.

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
