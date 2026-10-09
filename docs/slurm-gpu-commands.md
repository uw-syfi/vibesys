# GPU commands through Slurm

`--run-environment slurm-gpu` keeps the agent on the Slurm submit host and
sends only GPU processes to Slurm. The agent holds no GPUs while it reads,
edits, or thinks. Each GPU command gets its own job, sized and timed for that
command.

Use it on a host that is also a Slurm submit node with a shared view of the
project, for example a single-node cluster whose controller runs on the GPU
host. For remote clusters, use `--run-environment skypilot` (see
[Remote Slurm execution](remote-slurm-execution.md)).

## Agent view

The agent sandbox binds no GPU device nodes and sets `CUDA_VISIBLE_DEVICES=""`.
To use GPUs, the agent runs:

```bash
vibesys-gpu --gpus N --time MINUTES -- COMMAND...
```

The launcher's path is also in `$VIBESYS_GPU`. It applies to tests,
benchmarks, profilers (`ncu`, `nsys`), and any other CUDA process. Output
streams back as the job produces it. The exit status is the command's.
Interrupting the client cancels the job, whether it is queued or running.

The sandbox cannot reach the Slurm controller itself. The launcher asks a
host-owned broker over a private Unix socket with a per-run token. The broker:

- checks that the working directory is inside the run's workspace or one of its
  candidate worktrees;
- checks the request against the operator limits, and rejects a request that
  exceeds them rather than clamping it;
- runs the command with `srun`, wrapped in the same confinement policy as the
  agent, so the job sees the same filesystem view and write permissions.

The job gets the allocation's devices from Slurm. The broker drops the agent's
`SLURM_*` and device-visibility variables before it submits the job.

The trusted accuracy and benchmark gates run unconfined on the host, as in the
local environment, through the same `srun` launcher. Each uses the task's
`[resources]` accelerator count, or `gate_gpus` when the task declares none,
and the time limit `gate_time_minutes`.

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
- Jobs must run on a node that shares the workspace path and `/tmp` with the
  submit host: the benchmark gate writes its result under `/tmp`. Multi-node
  clusters without a shared `/tmp` are not supported yet.
- Gates run on a single node.
