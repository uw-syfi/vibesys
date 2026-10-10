# Agent GPU commands as an optional capability of the `slurm` environment

## Alternatives

Scores: + good, o neutral, - poor.

| Design | Guarantees | Where facts live | Cost per new capability | Trusted surface | Migration cost |
|---|---|---|---|---|---|
| (a) `[vibesys.agent_gpu]` table in the operator file, model owned by vs_sandbox, read through the existing policy loader; SlurmEnvironment composes the existing blocking `srun` launcher into its one broker | + unknown keys and limit violations rejected at load, named by key path; capability only with local transport (named error) | + one file, one loader (`load_slurm_policy`), one model | + one optional field on the policy, one optional `GpuCommands` on the broker | + same broker, same confinement, same token | + slurm-gpu becomes a different loader onto the same model; deletion removes the wrapper only |
| (b) GPU command jobs through the vs_slurm sbatch stack with log streaming | + one submission path, also works for ssh | o job state lives in vs_slurm | - needs a streaming/attach protocol in vs_slurm (step-1 owned), interactive latency of poll loops | o | - rewrites the launcher, cancel and output semantics; slurm-gpu behavior changes |
| (c) Separate capability object composed at the CLI entrypoint | o same checks, but entrypoint must know the environment internals | - split between entrypoint and environment | - every capability adds a CLI flag and an environment seam | - broker composition leaks to the entrypoint | - resume has to restore the capability separately |

Choice: (a). Override of the provisional pick: the TOML shape is `[vibesys.agent_gpu]`, not a
top-level `[agent_gpu]`. `[vibesys]` is already the operator file's VibeSys policy table and
`load_slurm_policy` already rejects unknown keys inside it, so the new table needs no change to the
document shape (a top-level table would have required changing the document model in
`slurm_policy.py` and any other reader that rejects unknown top-level keys). `vs_slurm` stays
credential-neutral job execution and does not learn about agent commands.

## Mechanism reuse (one implementation each)

| Piece | The one implementation | slurm-gpu after this change |
|---|---|---|
| Config model, limits, request validation | `vs_sandbox.slurm_gpu.AgentGpuConfig` (+ `.request`) | `SlurmGpuConfig(AgentGpuConfig)` adds only `gate_gpus`, `gate_time_minutes` |
| Config parsing | `[vibesys.agent_gpu]` in `SlurmExecutionPolicy` (slurm); `[slurm_gpu]` loader stays for the wrapper | loader dies with the wrapper |
| Broker composition | `HostCommandBroker(gpu=..., gates=...)`, built in one helper per environment from `agent_gpu_commands()` | same helper |
| Launcher writing and container env | `_agent_gpu_commands.py`: `agent_gpu_editor_env`, `agent_gpu_commands` (confinement + fail-closed check) | same |
| Editor open skeleton (paths, session, view) | `_host_command_bridge.open_bridged_editor` | same |
| Confinement | `HostJobConfinement` via `agent_gpu_commands` | same |
| Prompt facts and template | `SlurmEnvironmentFacts.agent_gpu` and `slurm/prompt_notes.j2` (conditional block) | emits `SlurmEnvironmentFacts`; `slurm_gpu/prompt_notes.j2` and `SlurmGpuEnvironmentFacts` deleted now |
| Gate runner | `SlurmCommandGateRunner` (sbatch) in slurm; `SrunGateRunner` stays only for the wrapper (next item deletes it) | unchanged, dies with the wrapper |

## Decisions

- `agent_gpu` is accepted only with `[slurm.transport] kind = "local"`: the blocking `srun` runs on
  this host and the job must see the workspace path, which only a submit node sharing a filesystem
  gives. ssh and connector transports are rejected with `SlurmPolicyError` naming `vibesys.agent_gpu`
  and the transport kind.
- Profiler: derived from the capability. With `agent_gpu`, the agent profiles through `vibesys-gpu`
  (nsys by default, ncu/rocprof as the backend dictates), profiling runs locally (`profile_execution`
  stays default), no remote capture plan is validated. Without it: rocprof via remote capture, as today.
  `materialize_local_model_weights` follows the same switch (jobs read the workspace directly).
- `gate_gpus` / `gate_time_minutes` are NOT in the shared model. The slurm environment sizes gate jobs
  by sbatch arguments and the evaluation plan; they exist only on the `slurm-gpu` wrapper's subclass.
- Gates stay on `SlurmCommandGateRunner` regardless of `agent_gpu`.
- One client launcher per run: `vibesys-gpu` when the capability is on (it serves `--gate` too),
  `vibesys-gate` otherwise.
- `parallel_candidate_blocker` is kept when the capability is on (unverified with one GPU broker per
  candidate); the plain slurm environment has none, as today.
