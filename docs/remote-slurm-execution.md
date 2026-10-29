# Remote Slurm execution

VibeSys can keep the editor in a local CPU container while running trusted
evaluators on a Slurm cluster through SkyPilot. The integration is site-neutral:
task manifests declare portable capacity, an operator profile supplies runtime
policy, and SkyPilot/SSH configuration supplies site access.

## Direct Slurm (`--run-environment slurm`)

`--run-environment slurm --slurm-config PATH` runs trusted accuracy and
benchmark evaluations, and ROCprof captures, on a Slurm cluster the submit host reaches
directly (for example over SSH), without SkyPilot. The editor is the same local
Docker container as in every other environment, with the workspace mounted at
its host path.

The container cannot hold the cluster's tools or credentials, so trusted work never
runs there. Every loop's agents request trusted evaluation through the evaluation
tool (`submit_evaluation`, then `validate_evaluation_wait`), which the run answers on
the host; only its results count, and the framework's own trusted evaluation of a
round runs the same stages on the cluster through the core evaluation executor. The
agents get no shell client for evaluation: no accuracy or benchmark command in their
paths and no evaluation command in their prompts. Without agent GPU commands the
container reaches no host broker at all.

The ROCprof server runs in the container and reaches the cluster through a
second host broker. It imports the Slurm adapter from the framework's `libs`
directory, mounted read-only at its host path, so the agent image needs only a
`python3` (3.11 or newer) with `pydantic`, which its MCP library already
requires. When the submit host is itself the machine that runs VibeSys, the
configuration can use the `local` transport (`[slurm.transport] kind = "local"`,
see the `vs-slurm` README) instead of SSH: evaluations run through the same
sbatch, poll and collect path. The local transport starts its programs in a
login-like environment (`HOME`, `USER`, `LOGNAME`, `SHELL`, `PATH`, `LANG`,
`LC_*`, `TZ`, `TMPDIR`, and nothing else), as sshd would, so the host's
credentials and any `SLURM_*` variables of an enclosing allocation never reach
a job. The profiler broker exists only for the SSH transport, so a
local-transport run without agent GPU commands offers no ad hoc ROCprof capture,
and no profiler.

A trusted profile capture needs the task's `[profile]` command. With an operator
`[vibesys.service]` the service runs under the profiler and that command is the load
driven against it. Without a service the command is itself the profiled target: it runs to
completion under the profiler and is never signalled, so it must exit by itself. A run that
cannot capture (no profile command, or no remote profiling) is not offered profile
workstreams, and its start log says so.

### Agent GPU commands (optional)

On a host that is itself a Slurm submit node (`[slurm.transport] kind = "local"`),
the operator can also let the agent run its own GPU commands as Slurm jobs, by
adding a `[vibesys.agent_gpu]` table to the same operator file. The same host
broker then offers `vibesys-gpu`, and the agent profiles
through its own commands instead of through the remote ROCprof capture. The
table is rejected with any other transport. See
[GPU commands through Slurm](slurm-gpu-commands.md) for the table, the agent's
view, and the deprecated `--run-environment slurm-gpu` alias.

The dynamic loop runs on `slurm` with or without agent GPU commands. Each
parallel candidate gets its own container session and its own host broker,
and a broker accepts only its own candidate's worktree, so one candidate's
agent cannot run a GPU job in another candidate's files.

## SkyPilot configuration ownership

- `vibesys.input.toml` owns portable requirements such as node count, accelerator
  count, and `cuda` or `rocm`.
- `~/.config/vibesys/clusters.toml` owns capacity and runtime policy such as the
  SkyPilot `infra`, accelerator model, allocation time, command prefix, and
  persistent remote artifact root.
- `~/.sky/config.yaml` and SSH configuration own accounts, partitions, proxies,
  certificates, keys, and site-specific Slurm translation.

A run never prompts on its terminal: the commands it starts have no terminal to
prompt on. The SSH transport runs `ssh` (and `rsync`'s remote shell) with
`-o BatchMode=yes`, so an `ssh` that would ask for a password, passphrase,
host-key confirmation or second factor fails at once, without sending an empty
answer to the server or opening an askpass window. The run then reports that ssh
could not connect or log in (ssh exit code 255). Make SSH access non-interactive
before the run, for example with keys in `ssh-agent`, known host keys, and a
`ControlMaster` connection opened beforehand; `ssh -o BatchMode=yes HOST true`
succeeds when it is.

Do not put credentials or account names in a task manifest or repository profile.
Install the validated external dependency separately:

```bash
uv tool install 'skypilot[slurm]==0.13.0'
```

## Allocation policy

A named SkyPilot cluster is an allocation lease, not a Slurm reservation. VibeSys
derives its name from the run and effective resources. It reuses an existing `UP`
lease, waits a bounded time for `INIT`, and creates a replacement when no reusable
lease exists. Evaluations are sequential within a run. The profile's
`allocation_time` can be `24:00:00`; normal shutdown releases the lease early.

Each evaluator invocation has a durable machine-local journal and immutable input
snapshot. Before submitting, VibeSys writes `PREPARED` and assigns a deterministic
job name. The journal binds that invocation to both request and snapshot digests.
VibeSys then persists `SUBMITTING` before invoking `sky exec`. On restart it queries
that exact name and persisted job ID before any new submission. An empty query for
a `SUBMITTING` invocation remains ambiguous unless the allocation is provably gone.
Completed results remain replayable until the sandbox client verifies and atomically
installs any artifact, then acknowledges delivery.
Each submitted attempt also freezes its effective profile, infrastructure, image,
and accelerator shape so a reattached result reports the resources that actually
ran it, even if resume selected another compatible profile.

Decoded remote stdout is written to a durable per-attempt spool before it is sent
to the sandbox client. If SkyPilot log following is interrupted, VibeSys reads the
job log again from its origin, verifies the saved prefix, and emits only the new
suffix. Separate journal cursors track remote reads and client delivery.

An allocation ending does not checkpoint an arbitrary evaluator process. If the
site or queue provides evidence that infrastructure interrupted the attempt, the
controller may create a bounded new attempt from the same snapshot. Application
failure, setup failure, and cancellation are terminal. A CLI timeout alone is
ambiguous and is never grounds to cancel, release, or blindly resubmit.

SkyPilot 0.13 does not return an idempotency token from `sky exec -d`. If submission
times out after remote acceptance but before the queue exposes the named job,
strict exactly-once submission cannot be proven. VibeSys preserves the journal and
requires later reconciliation instead of risking a duplicate.

State is local to the VibeSys host and is accessed through the run's opaque
machine-local state namespace:

```text
<machine-local-state>/runs/<run-id>/skypilot/
├── caller/
├── invocations/<invocation-id>.json
├── logs/<invocation-id>/<job-name>/stdout
└── snapshots/<invocation-id>/
```

## Launch and resume

```bash
vibesys --project /path/to/project --task TASK \
  --backend rocm --profiler none \
  --run-environment skypilot \
  --cluster-profile beverin-mi300a \
  --cluster-profiles-file ~/.config/vibesys/clusters.toml
```

The profile, profiles file, and SkyPilot executable are machine-local launch
choices. Supply them again when resuming on a new host:

```bash
vibesys --project /path/to/project --resume RUN_ID \
  --cluster-profile beverin-mi300a \
  --cluster-profiles-file ~/.config/vibesys/clusters.toml
```

See [`examples/config/skypilot-beverin.example.toml`](https://github.com/uw-syfi/vibesys/blob/main/examples/config/skypilot-beverin.example.toml)
for a credential-free Beverin template. Replace paths with operator-owned absolute
paths. VibeSys does not promise shell expansion in profile paths.

## Operations

Renew site certificates before launch and verify `sky status` and `sky queue` from
the host. Preserve `.vibesys/state/local` for recovery. Use SkyPilot to inspect a
stuck lease, but do not manually submit a second job with a VibeSys-owned name.
Normal VibeSys shutdown cleans up the lease; after a host crash, reconcile the
journal before manually bringing the cluster down.
