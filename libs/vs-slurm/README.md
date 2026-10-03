# vs-slurm

`vs-slurm` stages a local workspace, runs a command in a remote Slurm job, and
collects declared files and directories. Import its public API from
`vs_slurm.api`.

`SlurmJobRunner.submit_batch` runs ordered evaluation stages in one staged
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

The local machine needs OpenSSH and rsync. The login host needs rsync and the
Slurm commands named by the configuration, and `remote_workspace_root` must be
writable and visible from compute nodes.

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
