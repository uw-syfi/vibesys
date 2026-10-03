# SkyPilot-Backed Remote Slurm Execution Plan

Status: partially implemented. Phases 0 to 3 are implemented; Phase 4 (the end-to-end MI300A example) is pending.

## Goal

Let VibeSys agents edit candidates locally while trusted accuracy, profiling, and
benchmark commands run on a remote Slurm cluster. The first target is the CSCS
Beverin MI300A partition, but framework code must remain independent of CSCS,
Beverin, Slurm partition names, accounts, filesystem paths, and accelerator
models.

The target invocation is:

```bash
vibesys \
  --input examples/model-serving/llama-mi300 \
  --backend rocm \
  --run-environment skypilot \
  --cluster-profile cscs-mi300
```

An end-to-end run must support this sequence:

1. An agent edits an uncommitted local candidate.
2. Local CPU-only commands remain local.
3. A trusted evaluator command stages the current candidate and runs on the
   requested remote accelerators.
4. Logs, exit status, metrics, and declared artifacts return to the local run.
5. A later evaluation reuses compatible remote compute when possible.
6. Allocation expiry or node failure does not corrupt local experiment state.

## Design decisions

### Keep `RunEnvironment` as the framework boundary

The run environment owns the complete agent execution context: local sandbox,
path exposure, prompt notes, command rewriting, profiling location, and cleanup.
Add a `SkyPilotEnvironment` beside the existing local, Docker, and Modal
environments.

The environment starts a local editor sandbox without a local accelerator and
rewrites only trusted remote commands. Agent loops, Git tracking, round
transactions, and compute backends remain unaware of SkyPilot and Slurm.

### Keep remote job mechanics internal

Use a narrow internal `SkyPilotJobRunner` for the external CLI boundary. It
owns cluster lifecycle, task submission, log streaming, cancellation, and
result classification. It knows nothing about objectives, agents, rounds, or
judges.

Do not introduce a public, provider-neutral executor framework in the first
change. Modal and SkyPilot have different deployment and failure semantics.
Extract a shared remote job interface later only if another implementation
demonstrates the same contract.

### Treat the SkyPilot cluster as the allocation lease

On Slurm, a SkyPilot cluster is a long-lived Slurm job and `sky exec` runs work
on that allocation. Do not implement a separate `salloc`/`squeue` lease manager.
VibeSys needs only policy above SkyPilot:

- create or reuse one compatible logical cluster per campaign;
- run one remote evaluation at a time initially;
- recreate the cluster after allocation expiry or infrastructure failure;
- release it on normal campaign completion;
- rely on the Slurm wall time as final cleanup after local process failure.

Wall time is configurable. It is not fixed at 24 hours. Longer requests reduce
allocation churn but may increase queue latency. The initial Beverin profile
should request eight hours, permit up to the partition maximum, and release the
allocation early when the campaign finishes.

### Keep credentials on the host

The local agent sandbox must not receive the CSCS private key or unrestricted
SkyPilot credentials. A narrow host-side bridge owns SkyPilot and SSH access.
The evaluator helper in the editor sandbox communicates with that bridge over a
mounted Unix socket.

The bridge accepts a typed request containing the run identity, workspace
identity, command, timeout, allowed environment variables, and artifact
declarations. It resolves the configured profile itself. The sandbox cannot
provide arbitrary SSH hosts, Slurm accounts, partitions, or mount paths.

## Configuration ownership

Configuration has three owners.

### Portable experiment request

The experiment requests logical resources without naming a cluster or
provider:

```toml
[resources]
nodes = 1
accelerators_per_node = 4
accelerator_backend = "rocm"
```

The external schema must be a strict Pydantic model that rejects unknown keys.
The persisted run record stores the selected environment and portable resource
request. The effective operator profile is recorded as launch provenance, but
it is not an immutable resume constraint. A resumed run may select another
compatible profile. Transient cluster and job identifiers remain in
machine-local evaluation state.

### Launch selection

The operator selects the run environment and profile at launch:

```bash
vibesys \
  --input examples/model-serving/llama-mi300 \
  --backend rocm \
  --run-environment skypilot \
  --cluster-profile cscs-mi300
```

This selection belongs to launch configuration rather than the reusable task
bundle. It may be restored as a default on resume, but the operator can replace
it with another profile that satisfies the persisted portable resource
request.

### VibeSys operator profile

An operator-owned file maps the portable request to SkyPilot runtime policy:

```toml
# ~/.config/vibesys/clusters.toml

[profiles.cscs-mi300]
runner = "skypilot"
infra = "slurm/beverin/mi300"
accelerator_backend = "rocm"
accelerator_type = "MI300A"
accelerators_per_node = 4
cpus_per_node = 192
exclusive = true
command_prefix = [
  "srun",
  "--overlap",
  "--environment=/path/to/runtime-environment.toml",
]
allocation_time = "08:00:00"
remote_artifact_root = "/capstor/scratch/cscs/$USER/vibesys"
```

Profiles may constrain or fill portable requests but must fail clearly when a
request is incompatible. Core Python defaults must not contain the example
cluster name, account, partition, paths, GPU model, or image.
Provider- or cluster-specific launch wrappers such as the `srun` prefix above
belong only in the operator profile. Experiment manifests and framework code
must not encode them.

### Native SkyPilot configuration

SSH and Slurm translation remain in SkyPilot-owned configuration:

```yaml
# ~/.sky/config.yaml

slurm:
  cluster_configs:
    beverin:
      workdir: /capstor/scratch/cscs/$USER/skypilot
      tmpdir: /tmp/skypilot
      gpu_partition_map:
        MI300A: mi300
      sbatch_options:
        account: <slurm-account>
        exclusive: true
        time: "08:00:00"
```

SkyPilot SSH configuration uses the operator's signed SSH identity and jump
host. VibeSys should validate access and report renewal failures, but it should
not implement CSCS certificate issuance.

## Runtime flow

```text
local VibeSys process
  -> local Docker editor
  -> trusted evaluator helper
  -> host Unix-socket bridge
  -> SkyPilotJobRunner
  -> SkyPilot cluster and job APIs
  -> SSH login node
  -> Slurm allocation
  -> Pyxis/Enroot ROCm container
  -> structured result and artifacts
```

The host bridge performs these operations for each evaluation:

1. Validate the request against the selected operator profile.
2. Resolve a stable cluster name from the run ID and resource fingerprint.
3. Inspect the named SkyPilot cluster.
4. Reuse it if it is running and compatible, otherwise launch it.
5. Stage the current candidate.
6. Submit the command with `sky exec`.
7. Stream stdout and stderr to the evaluator and local logs.
8. Record the remote job identity and structured outcome.
9. Download declared result artifacts.

## Candidate staging

The first version supports one active remote evaluation per campaign and sets
`supports_parallel_candidate_evaluation` to false. This avoids concurrent
updates to SkyPilot's shared work directory.

Before each evaluation, stage the candidate visible to the agent, including
uncommitted changes. Exclusions must derive from the existing project path
policy and must exclude credentials, local run state, caches, logs, and large
model weights. Models and reusable caches belong on configured persistent
storage rather than in the candidate transfer.

Run commands at a stable remote container path such as `/workspace`. Evaluators
must not synchronize remote source mutations back into the candidate worktree.
Return only declared artifacts and framework-owned result metadata.

Before enabling parallel candidate evaluation, replace the shared mutable
workdir with immutable, per-invocation snapshots keyed by candidate revision
and evaluation invocation ID.

## Result and failure contract

The job runner returns a typed result rather than exposing raw SkyPilot output:

```python
JobResult(
    status=JobStatus.COMPLETED,
    exit_code=0,
    stdout_path=...,
    stderr_path=...,
    artifacts=(...),
    cluster_name="vibesys-...",
    remote_job_id="...",
    attempt=1,
)
```

Required statuses are:

- `COMPLETED`: return the application exit code and results.
- `APPLICATION_FAILED`: return failure without automatic retry.
- `ALLOCATION_EXPIRED`: recreate compatible compute and retry within policy.
- `NODE_FAILED` or `PREEMPTED`: recreate and retry within policy.
- `TRANSPORT_LOST`: reconnect and inspect remote state before deciding.
- `CANCELLED`: propagate cancellation and stop the active remote job.

Retries are bounded. An arbitrary nonzero application exit code is never
treated as infrastructure failure. Local VibeSys project state and round
transactions remain authoritative; the remote cluster is disposable.

## Implementation phases

### Phase 0: Beverin compatibility spike

The compatibility spike validated a bare SkyPilot allocation with workload
commands launched inside it through nested `srun --overlap` and the site EDF /
Pyxis runtime configuration. The allocation was reused across commands. Direct
SkyPilot `image_id` execution was incompatible with the CSCS runtime, so the
operator-owned command prefix is the supported container entry point there.

The validated procedure was:

1. Install pinned SkyPilot 0.13 with Slurm support in an isolated environment:
   `uv tool install 'skypilot[slurm]==0.13.0'`.
2. Configure the CSCS SSH certificate, Ela jump host, and Beverin login node.
3. Map logical `MI300A` resources to the untyped `gpu:4` GRES on `mi300`.
4. Launch a short one-node allocation under the configured account.
5. Run two commands through `sky exec` after separate local file changes.
6. Validate ROCm device discovery, PyTorch tensor execution on all four GPUs,
   Pyxis image startup, log streaming, allocation reuse, and teardown.

This phase is a go/no-go gate. If SkyPilot cannot support MI300A or the CSCS
Slurm configuration, retain the environment and result contracts but replace
the job runner with a focused SSH/Slurm implementation.

### Phase 1: Typed configuration and SkyPilot adapter

1. Add strict models for cluster profiles and portable resource requests.
2. Extend persisted run-environment records and resume validation.
3. Implement an injectable subprocess boundary for the SkyPilot CLI.
4. Implement resource translation, stable naming, status inspection, launch,
   execution, cancellation, and release.
5. Translate missing CLI, timeout, malformed output, and nonzero control-plane
   failures into typed errors without exposing credentials.

### Phase 2: Host bridge and run environment

Implemented by the `SkyPilotEnvironment`, versioned host bridge, and
sandbox-side evaluator helper. Remote profiling remains disabled until it has
its own typed request and artifact contract. Durable invocation recovery and
reattachment remain Phase 3 work.

1. Add the versioned Unix-socket request and response contract.
2. Start and stop the host bridge with the run environment session.
3. Add the sandbox-side evaluator helper.
4. Add `SkyPilotEnvironment` using the existing local Docker editor path with
   accelerator attachment disabled.
5. Rewrite accuracy, benchmark, and supported profiler commands.
6. Render remote runtime notes from typed capabilities and profile data.
7. Make teardown deterministic and idempotent on success, failure,
   cancellation, and partial startup.

### Phase 3: Persistence and recovery

Implemented by the machine-local `PREPARED`/`SUBMITTING` invocation journal,
digest-bound immutable snapshots, deterministic job reconciliation, decoded log spools, bounded
infrastructure-only retries, and acknowledged artifact delivery.

1. Persist evaluation invocation records under framework-owned local run state.
2. Stream logs incrementally and resume from recorded offsets after reconnect.
3. Reattach to running jobs after local interruption when possible.
4. Relaunch expired allocations and retry only infrastructure-interrupted work.
5. Download declared artifacts and record resource, image, cluster, and job
   provenance with the evaluation result.

### Phase 4: End-to-end MI300A example

1. Add or retarget a model-serving task whose objective and constraints match
   ROCm and MI300A rather than H100/Hopper.
2. Validate a minimal SGLang or vLLM server and client benchmark manually.
3. Run one complete VibeSys round.
4. Run multiple rounds that reuse the same SkyPilot cluster.
5. Validate recovery after forced allocation expiry.
6. Document operator setup, certificate renewal, normal teardown, and manual
   cleanup.

## Verification

### Unit tests

- Accept valid profiles and reject unknown or incompatible fields.
- Translate portable resources to the expected SkyPilot task.
- Produce stable cluster names and resource fingerprints.
- Build external commands as argument sequences.
- Wrap only the intended agent-visible commands.
- Classify application and infrastructure failures correctly.
- Relaunch after simulated expiry within the retry bound.
- Never retry ordinary application failure.
- Make cancellation and teardown idempotent.
- Redact credentials and sensitive environment values from diagnostics.

### Integration tests

Use a fake SkyPilot executable or injected runner to cover:

- launch once, execute twice, and release once;
- reuse an existing compatible cluster;
- reject an incompatible existing cluster;
- reconnect to a running job;
- replace an expired allocation;
- stream logs and return declared artifacts;
- cancel an active step without leaking the campaign allocation.

### Live acceptance test

The feature is complete when a local agent can:

1. modify an uncommitted candidate;
2. invoke its normal trusted benchmark command;
3. receive a result measured on Beverin MI300A GPUs;
4. modify the candidate again;
5. reuse the same allocation for the next measurement;
6. finish or resume the VibeSys run with complete local state after the remote
   allocation is released or replaced.

## Non-goals

- Running agent reasoning or file editing on compute nodes.
- Encoding CSCS policy or Beverin names in framework defaults.
- Replacing Slurm scheduling, fair share, or accounting.
- Building a general workflow engine.
- Supporting parallel candidate evaluations in the first version.
- Supporting SkyServe on Slurm.
- Automatically issuing or renewing site-specific SSH certificates.
- Automatically retrying failed candidate code.

## Documentation and rollout

Keep the implementation changes narrow and independently reviewable:

1. Compatibility spike and operator notes.
2. Typed configuration and SkyPilot job runner.
3. Host bridge and `SkyPilotEnvironment` integration.
4. Recovery, provenance, and MI300A end-to-end example.

After the compatibility spike, pin the validated SkyPilot version. Treat an
upgrade as an external integration change and rerun the live smoke tests before
changing the pin.

Relevant upstream documentation:

- [SkyPilot Slurm setup](https://docs.skypilot.ai/en/latest/reference/slurm/slurm-getting-started.html)
- [SkyPilot Slurm migration mapping](https://docs.skypilot.ai/en/latest/reference/slurm-migration.html)
- [SkyPilot task YAML](https://docs.skypilot.ai/en/stable/reference/yaml-spec.html)
- [SkyPilot advanced Slurm configuration](https://docs.skypilot.ai/en/stable/reference/config.html)
