# GPU commands through Slurm

`--run-environment slurm-gpu` runs the agent in a local Docker container on the
Slurm submit host and sends only GPU processes to Slurm. The agent holds no
GPUs while it reads, edits, or thinks. Each GPU command gets its own job, sized and timed for that
command.

Use it on a host that is also a Slurm submit node with a shared view of the
project, for example a single-node cluster whose controller runs on the GPU
host. For remote clusters, use `--run-environment skypilot` (see
[Remote Slurm execution](remote-slurm-execution.md)).

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
`vibesys-gpu --gate benchmark`; the broker runs the planned command unconfined
through the same `srun` launcher. Each uses the task's `[resources]` accelerator
count, or `gate_gpus` when the task declares none, and the time limit
`gate_time_minutes`. A benchmark's result file under `/tmp` is written on the
host, so the broker relays it back into the container.

## Operator configuration

The configuration is machine-local. The default path is
`~/.config/vibesys/slurm-gpu.toml`; `--slurm-config PATH` overrides it.

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
