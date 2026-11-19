# Placements: telling the CLI about the machine

A placement is a named place where a job runs, with what the machine gives it: its GPUs, a
Kubernetes cluster, read-only mounts. The operator config lists them. It is the user's file: it
holds facts about their machine, which no task and no agent decides. With no file, jobs run on this
machine with no GPU, so a task that needs `gpu` is refused naming it.

## Where it is, who changes it

`$VIBESYS_CLI_CONFIG` when set, otherwise `$XDG_CONFIG_HOME/vibesys/cli.toml`, otherwise
`~/.config/vibesys/cli.toml`. The CLI refuses unknown keys, naming them. The daemon reads the file
when it starts, so every change takes `"$VIBESYS_CLI" daemon restart`. Running jobs keep running.
The restart refuses (`CONFLICT`) while the daemon is answering a submission; wait, and add `--force`
only with the user's approval, since that submission then starts over.

Propose the text and say what it changes. Write it only when the user asks or approves ("set up my
GPUs"), and never to get past a refusal in `vibesys:optimize`. Before you edit an existing file,
copy it beside itself (`cli.toml.bak`) and show the difference afterwards. Put nothing in it that
the user did not tell you or the machine did not show: no guessed host name, partition or path.

## What to find out first

1. The GPUs: `nvidia-smi -L` (NVIDIA) or `rocm-smi --showid` (AMD). Note the count and the model.
   On AWS Trainium, `neuron-ls --json-output` (in `/opt/aws/neuron/bin`): note the number of devices
   and each one's `nc_count`, its NeuronCores.
   A pool is all of the machine's GPUs: `count` must equal the number the machine reports, and the
   CLI refuses a different number. If the user wants to keep some GPUs out, or the GPUs are of
   mixed types, say that this is not supported yet and ask what they want.
2. For Docker: `docker info --format '{{json .Runtimes}}'` should list `nvidia`. If it does not, a
   container cannot get a GPU until the NVIDIA container toolkit is installed; tell the user, and do
   not install it for them. A machine with no container runtime runs jobs on the host (below).
3. `"$VIBESYS_CLI" daemon status`. Its first line is this user's daemon: `not running` (exit 4) is
   normal before the first start, and otherwise it counts the queued and running jobs, which you
   check before a restart. The lines after it describe other daemons on the machine; ignore them.

## The file (version 0.2)

The file does not choose between Docker and the host. A job runs in a container when its task has a
`Dockerfile`, and on the host when the task has none or the job passes `--env host`; either way it
draws from the same pool of GPUs.

A file starts with `version = "0.2"`, the only version read. A file of the earlier shape is
refused, naming each old key and what to write instead. Each placement has a `kind`, and a GPU
table whose `count` is how many GPUs the machine has (for a cluster, each node). With a `count`
the GPUs `0` to `count - 1` are a pool that jobs share: each job asks for some with `--gpus N` and
is given that many of its own. The CLI checks `count` against what the machine reports and refuses
to start when they differ.

Local machine, Docker, NVIDIA:

```toml
version = "0.2"

[placements.local]
kind = "local"
[placements.local.resources.gpu]
count = 8
```

A machine with no container runtime (a pod, say): kind `local` needs no Docker. Its jobs run on the
host.

```toml
version = "0.2"

[placements.pod]
kind = "local"
[placements.pod.resources.gpu]
count = 8
```

Another machine over ssh, Docker (one job at a time):

```toml
version = "0.2"

[placements.box]
kind = "ssh"
[placements.box.remote]
host = "host-a"
remote_workspace_root = "/scratch/vibesys"
[placements.box.resources.gpu]
count = 4
```

Slurm with GPU resources (GRES): Slurm keeps jobs apart and each job's allocation asks for the
GPUs it needs. Add `gres_type` only when the partition names or mixes GPU types.

```toml
version = "0.2"

[placements.spark]
kind = "slurm"
[placements.spark.remote]
host = "host-b"
remote_workspace_root = "/scratch/vibesys"
[placements.spark.slurm]
partition = "gpu"
[placements.spark.resources.gpu]
count = 8              # GPUs per node
# gres_type = "a100"
```

A Slurm cluster without GPU resources: leave `gres_type` out. Each job's allocation is a whole
node, and the CLI names the job's devices inside it with variables.

```toml
version = "0.2"

[placements.mi300]
kind = "apptainer"
[placements.mi300.remote]
host = "login-a"
remote_workspace_root = "/shared/vibesys"
[placements.mi300.slurm]
partition = "mi300"
[placements.mi300.resources.rocm]
count = 8              # GPUs per node, no gres_type
```

An AWS Trainium machine (local or ssh): its pool counts NeuronCores, not devices. `count` is the sum
of `nc_count` over the devices and `cores_per_device` one device's `nc_count`; the CLI checks both.
A task of the `trainium` backend needs `neuron`.

```toml
version = "0.2"

[placements.trn]
kind = "local"
[placements.trn.resources.neuron]
count = 4              # a trn2.3xlarge: one device of 4 NeuronCores
cores_per_device = 4
```

For the other keys (accounts, time limits, mounts, standing allocations) read
[the operator config in the CLI README](https://github.com/uw-syfi/vibesys/blob/main/cli/README.md#the-operator-config).
Ask the user for each fact; never invent one.

## What jobs then do

- A check or command whose task needs GPUs says `--gpus N`: required, no default, 1 to `count`. A
  refusal names the sizes. A measure takes the whole node, alone, and refuses `--gpus`.
- Jobs wait for free GPUs and run side by side on disjoint ones. `vibesys-cli placement capacity
  <placement>` shows free GPUs and who holds the rest; `jobs` shows what each job asked for.
- A job reads `VIBESYS_GPUS` and `VIBESYS_GPU_DEVICES`, and the vendor's own variable
  (`CUDA_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES`, `NEURON_RT_VISIBLE_CORES`). On a Neuron pool
  `--gpus N` counts NeuronCores, and a job gets N in a row.
- A Neuron job does not start while another process holds its cores: it fails naming the device
  and, when it can see them, the holders' pids. `neuron-ls` shows which processes hold
  the device. Tell the user; do not kill processes you did not start.
- Confinement is best effort. A Docker job gets its NVIDIA devices by number, so it cannot see
  the others. A host job and an AMD job get variables only: a process that ignores them can open any
  GPU, and so can a process the CLI did not start.

## Check that it worked

1. `"$VIBESYS_CLI" daemon restart`. A refusal names the key it did not accept; fix that key. A
   restart that succeeds means the file was accepted, `count` included.
2. `"$VIBESYS_CLI" placement capacity <placement>` must say `N of N free` for the GPUs you declared.
   This needs no task.
3. Look for a task that needs GPUs: `ls .vibesys/tasks` in the user's repository. If there is one,
   commit any pending change to it (a job runs a commit), then submit a job that prints what it
   sees:
   `"$VIBESYS_CLI" submit command <sha> --task <name> --image harness --gpus 1 --wait -- sh -c 'echo $VIBESYS_GPU_DEVICES; nvidia-smi -L'`
   (`rocm-smi --showid` on AMD; `echo $NEURON_RT_VISIBLE_CORES` on Trainium). `--task` is always needed (or set `VIBESYS_TASK`), even with one
   task. `--image harness` runs the command in the task's own environment. Add `--env host` when
   the task has no `Dockerfile` or the machine has no container runtime. `--wait` prints the job's
   state, not its output: read the output with `"$VIBESYS_CLI" logs <job>`, which must show one GPU,
   and `"$VIBESYS_CLI" jobs --task <name>` must show the device it ran on.
4. Say which placements you configured and what you saw. If there is no task, say the job check is
   still to do.
