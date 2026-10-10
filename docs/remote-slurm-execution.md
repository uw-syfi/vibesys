# Remote Slurm execution

VibeSys can keep the editor in a local CPU container while running trusted
evaluators on a Slurm cluster through SkyPilot. The integration is site-neutral:
task manifests declare portable capacity, an operator profile supplies runtime
policy, and SkyPilot/SSH configuration supplies site access.

## Direct Slurm (`--run-environment slurm`)

`--run-environment slurm --slurm-config PATH` runs trusted accuracy and
benchmark gates, and ROCprof captures, on a Slurm cluster the submit host reaches
directly (for example over SSH), without SkyPilot. The editor is the same local
Docker container as in every other environment, with the workspace mounted at
its host path.

The container cannot hold the cluster's tools or credentials, so the host owns a
broker and the container reaches it over a bind-mounted Unix socket with a
per-run token. The agent runs a gate as `vibesys-gate --gate accuracy` or
`vibesys-gate --gate benchmark`. The client is mounted at
`/usr/local/bin/vibesys-gate`, so the bare name resolves in any shell, and the
environment notes in the agent's prompts name it and the gates the task plans. The
broker runs the planned gate with the
cluster wrapper (`vs_sandbox.slurm_command`), which stages the workspace, runs
the job, and copies the result back. The agent names only the gate and, for a
benchmark, a result path; it cannot supply a command. Interrupting the client or
dropping the connection cancels the job.

The ROCprof server runs in the container and reaches the cluster through a
second host broker. It imports the Slurm adapter from the framework's `libs`
directory, mounted read-only at its host path, so the agent image needs only a
`python3` (3.11 or newer) with `pydantic`, which its MCP library already
requires. When the submit host is itself the machine that runs VibeSys, the
configuration can use the `local` transport (`[slurm.transport] kind = "local"`,
see the `vs-slurm` README) instead of SSH: gates run through the same
sbatch, poll and collect path. The second broker exists only for the SSH
transport, so a local-transport run offers the gates but not the agent's ad hoc
ROCprof capture. The GPU-side variant for a single-node cluster is described in
[GPU commands through Slurm](slurm-gpu-commands.md).

## SkyPilot configuration ownership

- `vibesys.input.toml` owns portable requirements such as node count, accelerator
  count, and `cuda` or `rocm`.
- `~/.config/vibesys/clusters.toml` owns capacity and runtime policy such as the
  SkyPilot `infra`, accelerator model, allocation time, command prefix, and
  persistent remote artifact root.
- `~/.sky/config.yaml` and SSH configuration own accounts, partitions, proxies,
  certificates, keys, and site-specific Slurm translation.

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
