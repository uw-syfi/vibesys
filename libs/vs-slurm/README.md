# vs-slurm

`vs-slurm` stages a local workspace, runs a command in a remote Slurm job, and
collects declared files and directories. Import its public API from
`vs_slurm.api`.

`Cluster.submit(request, operation_id=...)` accepts single-command or batch requests.
The caller chooses and persists the operation ID before submission. Import
`Cluster` and its typed outcomes from `vs_slurm.api`; composition code imports
`SlurmCluster` and `FakeCluster` from `vs_slurm.wiring`. The production
implementation wraps `SlurmJobRunner` and records its submission ledger under
an explicit local `state_root`. Preserve that directory across restarts. An
atomic claim in the configured remote workspace also binds the operation ID to
its payload across independent local state directories. Inspection can recover
remote acceptance evidence into a fresh local ledger. An interrupted claim
remains Unknown and does not authorize another scheduler submission.

An identical submission returns the same logical job. A different payload under
the same ID returns `ClusterConflict`. Transport loss after scheduler dispatch
returns `ClusterUnknown` with the operation ID. Inspect that ID before retrying;
a missing scheduler record cannot prove that a dispatched submission was rejected.
Cancellation acknowledges a request, and a separate inspection confirms termination.
Inspection exposes pending reason and estimated start when Slurm supplies them.
Collection preserves completed-stage artifacts even if the allocation fails or
its exit status is missing. Missing status remains Unknown, with available
evidence retained, and never becomes exit code zero.

The underlying `SlurmJobRunner.submit_batch` runs ordered evaluation stages in one staged
workspace, allocation, and service lifecycle. Stages return separate stdout,
stderr, exit status, elapsed time, and declared artifacts. `stop_on_failure`
defaults to true, so later stages are marked skipped after the first nonzero
stage result. Batch handles use the same poll, bounded wait, cancel, and
recovery model as single-job handles. Persist the updated handle returned by
`wait_batch` to retain accumulated wait-observation time across process restarts.

Workspace and support-tree transfers use content-addressed objects under the
configured remote workspace root. A complete upload is atomically published
with its ready marker. Each job receives a fresh writable workspace copied
from those immutable objects, so a job cannot change cached inputs. Workspace
hashes exclude `.git/`, matching the staged snapshot; support-tree hashes
include all files. Concurrent publishers upload to unique temporary paths, then
serialize publication with a per-digest lock. A lock is reclaimed only after
five minutes, when its recorded publisher process is gone and no target object
exists. A bounded lock wait returns a retryable staging error.

Batch timing metadata includes local staging and submission durations, cache
hit count, accumulated
wait-observation duration, remote setup and service startup durations,
per-stage elapsed time, and collection duration. Slurm sites do not expose a uniform queue-start
timestamp through this API, so wait-observation time is not reported as queue
time.

## Configuration

The standard transport uses the installed `ssh` and `rsync` commands. Put
credentials, jump hosts, and site-specific connection settings in
`~/.ssh/config` or an SSH agent. Keep them outside the VibeSys repository.

```toml
[slurm]
name = "my-cluster"
remote_workspace_root = "/work/my-user/vibesys"
sbatch_arguments = ["-p", "gpu", "-N", "1", "-t", "00:20:00"]

[slurm.transport]
kind = "ssh"
host = "my-cluster"
```

`host` is an OpenSSH host name or config alias. `sbatch_command` defaults to
`["sbatch"]`; `ssh_command` and `rsync_command` default to the corresponding
commands on `PATH`. Every transport process has the hard deadline configured by
`transport_timeout_seconds`.

`dedicated_measurement_resources` (default `false`) declares that each
measurement job gets its GPUs and CPU cores to itself through the scheduler,
though it may share the host with other jobs. Set it for a shared GPU node
where `sbatch_arguments` allocate whole GPUs and cores (for example
`--gres=gpu:4 -c 64`) but cannot pass `--exclusive` without taking the whole
node from other users. A bare `--exclusive` as the last node-sharing option in
`sbatch_arguments` implies it (`--exclusive=user`, `--exclusive=mcs`,
`--exclusive=topo`, `--oversubscribe`, `--share` and `-s` share nodes, so they
do not count). When it holds, an official measurement of a task that opts in
may reuse the agent's own run of the identical input instead of queuing a
second job; leave it `false` unless the declaration is
true, because it is the only evidence that the two runs faced the same
conditions.

Every job is submitted with `--no-requeue`, after the operator's
`sbatch_arguments`, and `--requeue` there is rejected: VibeSys retries a failed
job itself, and a scheduler requeue would rerun stages that already finished.
A job's service and its command each run in a session of their own (so the
compute node needs `setsid`, which util-linux provides). When the job script
exits, it sends SIGTERM to every process of both sessions, SIGKILL
`service_stop_grace_seconds` later (1 to 20, default 20), and then waits until
none of them is left, for at most `process_exit_wait_seconds` (1 to 1800,
default 120). A process Slurm finds still running when the script has exited
can make Slurm's step kill fail ("Kill task failed") and drain the node; a CUDA
process holding a large GPU allocation can take tens of seconds to exit after
SIGKILL. A zombie does not count as running, unless it still has threads: a
process whose main thread exited shows as a zombie while its other threads run,
and a CUDA process releases the GPU only when its last thread exits. When a
process outlives the wait, the script lists it (`ps` output) on the job's stderr
and exits with code 71 (`PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE`), so the
evaluation is not reused; a service that never became ready keeps its code 70.
Both commands start with the environment variable `VS_SLURM_JOB_PROCESS` set to
a token unique to the job script, so a descendant that left their sessions with
`setsid` (Ray and daemons do) is found by that marker, and its own session joins
the job's; a process found that way gets SIGTERM too while the grace lasts. A
process also needs the job's own `SLURM_JOB_ID` to match, so a nested job that a
command submitted, which inherits the marker, is left alone. Not found: a
descendant that leaves the sessions and also clears its environment
(`env -i setsid ...`), or that is already exiting when teardown starts; Slurm's
own step kill still ends it, but the script does not wait for it.
Reading another process's environment can block while that process is stuck in
the kernel, so each read, and the `ps` report, is cut off after a few seconds.
A process of the job's user whose environment read was cut off for the whole
wait counts as possibly the job's: the script names it and exits with code 71.

The grace plus the wait must be shorter than a `--time`/`-t` limit in
`sbatch_arguments`: an explicit wait that does not fit is rejected, and the
default wait shrinks to the limit minus the grace minus 1 second. VibeSys's
own run deadline, `job_timeout_seconds`, is not checked against them, but should
leave the same room after the command ends: a cancel during teardown no longer
interrupts the script, and Slurm kills it KillWait (30 s by default) later.
Slurm's own SIGTERM, at a cancel or the time limit, runs the same teardown, and
the grace keeps the script's SIGKILL 10 s ahead of Slurm's default KillWait.
A setup script is sourced into the job script itself, so a background process
it starts is not part of either session and is left to Slurm. What the setup
script exports reaches the service and every stage, including a stage that is a
`bash -c`: the job script unsets `SSH_CLIENT` and `SSH2_CLIENT` first, because a
`bash` built for sshd that sees them re-reads the login rc files and re-exports
the login values over the setup's.

The local machine needs OpenSSH and rsync. The login host needs rsync and the
Slurm commands named by the configuration, and `remote_workspace_root` must be
writable and visible from compute nodes.

A host that is itself a Slurm submit node uses the local transport: scheduler
and filesystem commands run through `shell_command` on this host, and
transfers are local `rsync` copies.

```toml
[slurm.transport]
kind = "local"
# shell_command = ["bash", "-c"]   # receives the command line as its last argument
# rsync_command = ["rsync"]
```

`remote_workspace_root` must then be a directory this host shares with the
compute nodes. A wrapper may precede the shell in `shell_command`, for example
one that runs the shell on a login node the host reaches without SSH.

Every program the local transport starts (`shell_command` and `rsync_command`)
runs in a login-like environment, not this process's: only `HOME`, `USER`,
`LOGNAME`, `SHELL`, `PATH`, `LANG`, `LC_*`, `TZ` and `TMPDIR`, when set, and
nothing else. This is what the SSH transport gets from sshd. A host process can
hold credentials, and one that runs inside an allocation or batch job holds
`SLURM_*` variables that would point the gate's own `sbatch` at the wrong job;
none of it reaches the programs. `sbatch` itself is submitted exactly as over
SSH, so a job gets the environment Slurm's defaults give it.

The local transport runs whatever `shell_command` names with the host's
privileges, so it is for the host's own trusted code only. VibeSys therefore
does not expose it through the editor's host-side transport broker: doing so
would turn a remote-capture capability into arbitrary host shell execution.
An explicit ROCprof selection is rejected during profiler validation, while
`--profiler auto` selects no profiler. Gates still run through the full job
path.

Sites whose gateway is not reachable through OpenSSH can provide the advanced
versioned JSON connector transport:

```toml
[slurm.transport]
kind = "connector"
command = ["site-slurm-connector"]
```

The connector receives one JSON request on standard input and returns one JSON
response on standard output. This escape hatch is intended for operator-managed
gateways; normal SSH-based clusters do not need it.

The ROCprof server reaches either SSH or connector transports through the same
host-owned broker. For a connector, the broker runs only the exact configured
command and accepts only the version-1 request schema. Every transfer's local
path must remain under the run-owned roots and every remote path under
`remote_workspace_root/name`; unknown keys, operations, versions, and
non-normalized paths are refused before the connector starts. Local path
spellings containing `.` or `..` components are invalid. The broker walks a
local path from its authorized root without following links and gives the
transport only a private staged snapshot; successful downloads are published
back through the same descriptor-rooted boundary. Staging retains regular-file
and directory modes and modification times, including executable bits.
Replacing a checked path can therefore neither change an upload nor redirect a download. Connector
credentials remain owned by the host process and are not mounted into the
editor container. A complete connector response, including its JSON envelope,
is limited to 1 MiB so the two brokered process streams always fit the broker's
bounded response frame.

## Recoverable job lifecycle

`SlurmJobRunner.submit()` returns a credential-free `SlurmJobHandle`. Persist
`handle.model_dump_json()` with the caller's run state, then validate it with
`SlurmJobHandle.model_validate_json()` after restart. A runner using the same
cluster name, workspace root, and transport can poll, wait, cancel, or collect
that operation without submitting it again.

`wait(handle, timeout_seconds=...)` returns a `SlurmJobWaitResult`. If
`timed_out` is true, the job remains submitted and can be observed again later;
the bounded wait does not cancel it. Call `cancel(handle)` to request scheduler
cancellation. Call `collect(handle)` after a terminal state to retrieve output
and declared artifacts. The blocking `run(request)` method remains available
and composes these operations with the configured job deadline.
