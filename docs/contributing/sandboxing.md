# Sandboxing and confinement

"Sandbox" used to name several unrelated things. This page fixes the
vocabulary, says who owns each piece, traces the configuration of each layer,
and lists what is and is not enforced. When code and this page disagree, fix
whichever is wrong in the same PR.

## Vocabulary

| Term | Meaning | Code |
| --- | --- | --- |
| run environment | Where a run's evaluations execute: local Docker, Modal, SkyPilot, Slurm, host. The agent always runs in a local Docker container; Slurm's editor and host-only backends on macOS (Metal) are the exceptions (see Known limits). | `RunEnvironment` in `vs_runtime` |
| command runner | A handle that executes one shell command and returns a bounded result. It isolates nothing. | `CommandRunner`, `CommandResult`, `LocalShellRunner`, `DockerSandbox.execute` in `vs_sandbox` |
| agent confinement | Restricting the agent CLI process: which paths it can read and write, and which it cannot see. "Sandbox" is reserved for this. | `WorkspaceSandbox` and `HostSandbox`, `LandlockSandbox`, `SeatbeltSandbox`, `DockerSandbox.wrap` |
| candidate isolation | Per-workstream evaluation isolation: each candidate is evaluated in its own worktree or workspace so candidates cannot affect each other's results. | `_project_run.py`, `_trusted_evaluation.py` in `vs_runtime` |
| trust boundary | The protections that keep the evaluator, its inputs and the run's own state out of the agent's reach: read-only and hidden project paths, plus the accuracy gate that diffs protected paths against a trusted baseline. | `ProjectPathPolicy` |
| candidate build | A task's `[build]`: candidate code the framework runs before the trusted check and measure, confined like the agent, in a fresh checkout of the measured commit. Its declared outputs are verified and recorded outside every workspace. | `TrustedBuild`, `RuntimeWorkspaceEvaluation.build`, `_build_outputs.py` in `vs_runtime` |

A class name containing "Sandbox" must be a confinement type;
`tests/architecture/test_sandbox_naming.py` enforces it.

Two traps:

- `RunEnvironment.isolated` and `RunEnvironmentView.isolated` mean "the run
  uses a container workspace", not candidate isolation.
- `DockerSandbox` is both a command runner (`execute`) and a confinement
  (`wrap`, `agent_path`). The framework's own file operations use `execute`;
  agent turns are launched through `wrap`. The name stays because it does
  confine.

## Who owns what

| Concern | Owner |
| --- | --- |
| Confinement policy and enforcement (which paths, which mechanism, fail closed) | VibeSys: `vs_sandbox` mechanisms, `vs_agent` wiring |
| Provider knowledge (argv, state directories, auth variables, resume) | agentshim |
| Provider CLI built-in sandboxes and approvals | Intentionally off |
| Run environment choice and evaluator dispatch | `vs_runtime` |

agentshim is a transport. VibeSys wraps each provider argv before launch:
`confine_to_sandbox` in `libs/vs-agent/src/vs_agent/session_launch.py`
rewrites every command through the confinement's `wrap`, on the host or in a
container alike. The provider CLIs run with approvals and their own sandboxes
off (`--dangerously-bypass-approvals-and-sandbox`,
`--dangerously-skip-permissions`), so VibeSys confinement is the only boundary.
Never rely on a provider flag for isolation. See
[Agent sessions](agent-drivers.md#where-agentshim-lives) for the library split.

## Configuration flow per layer

1. **Run environment.** `--modal`, `--run-environment` (default `docker`) select a
   `RunEnvironment`. `--docker` and `--run-environment local` are rejected. It builds command runners through
   `ComputeBackendImpl.make_sandbox(kind, ...)`, where `SandboxKind` is only
   `LOCAL` or `DOCKER`: where the framework's shell commands execute. Modal,
   SkyPilot, and Slurm are run environments, not kinds.
2. **Remote editors.** Modal, SkyPilot and Slurm start a local
   Docker editor with `attach_accelerator=False` (a control plane without
   devices). Heavy evaluation dispatches through the candidate's `modal run`
   entrypoint, a SkyPilot job, or a Slurm job, not through a command runner.
   The Slurm environments cannot give the container the cluster tools and
   credentials, so a host-owned command broker runs the trusted gates (and, for
   the agent's GPU jobs, when the operator enables `[vibesys.agent_gpu]`) and the container reaches it over a
   bind-mounted Unix socket; the workspace is mounted at its host path so
   directories mean the same thing on both sides. The ephemeral evaluator-tool
   builder also uses `attach_accelerator=False`.
3. **Project path policy.** `ProjectPathPolicy` lists read-only and hidden
   workspace-relative paths. It is validated once, then lowered by each
   confinement backend. `build_project_path_policy` (`src/vibesys/run/project_policy.py`)
   builds it; the framework state's part comes from `Project.state.sandbox_paths()`,
   which owns the layout. Every hidden path must exist in the workspace it is applied
   to, so `open_run_environment` first prepares the hidden state directories in a
   linked candidate worktree (`prepare_hidden_directories`).
4. **Agent session.** `AgentExecutionPolicy` carries the policy,
   `host_resources`, and `require_enforcement` into the session launcher. Run
   entrypoints set `require_enforcement = not use_docker`: a host (Metal) run
   must be confined or fail.
5. **Confinement.** On the host the launcher calls `build_host_sandbox` and gets
   `HostSandbox` (Linux, bubblewrap), `LandlockSandbox` (Linux, opt-in), or
   `SeatbeltSandbox` (macOS). In a container it uses the run's started
   `DockerSandbox`. Read-only and hidden paths reach a container through
   `docker_project_path_resources`: read-only paths are re-mounted read-only,
   hidden paths are overlaid with empty operator-owned masks.

### Operator controls

| Variable | Effect |
| --- | --- |
| `VIBESYS_AGENT_SANDBOX` | Linux mechanism: `auto` (default) and `bwrap` require bubblewrap, `landlock` opts in to the weaker backend. `0`, `false`, `off`, `no` request no confinement. Other values are rejected. |
| `VIBESYS_AGENT_SANDBOX_ALLOW` | `os.pathsep`-separated host paths granted to the agent read-only, in addition to the default resources. Host runs only; container mounts come from the run environment. |

Disabling is honored only when the caller does not require enforcement. Every
VibeSys run entrypoint requires it on the host, so `VIBESYS_AGENT_SANDBOX=0`
stops the run with `SandboxUnavailableError` instead of launching the agent
unconfined. Embedders that build an `AgentClient` with
`require_host_sandbox=False` (the library default) get the disable, with a loud
log line. Both variables reach the agent session through the environment
allowlist (`session_env_allowlist`).

## Support matrix

| Run environment | Agent confinement | Read-only paths | Hidden paths |
| --- | --- | --- | --- |
| Default (`docker`) | `DockerSandbox` | enforced (read-only re-mount) | enforced (empty mask mount) |
| `--modal`, `--run-environment skypilot` | `DockerSandbox` editor container | enforced | enforced |
| `--run-environment slurm` | `DockerSandbox` editor container; with `[vibesys.agent_gpu]`, each brokered GPU job runs on a compute node under host confinement | enforced in the container; per host for jobs | enforced in the container; per host for jobs |
| Host-only backend (Metal), macOS | `SeatbeltSandbox` | enforced | enforced |
| `--run-environment slurm-gpu` (deprecated alias of `slurm` with `[vibesys.agent_gpu]`) | as `slurm` with `[vibesys.agent_gpu]` | enforced in the container; per host for jobs | enforced in the container; per host for jobs |

### Run state is part of the trust boundary

Run state is the framework's, not the agent's. Below `.vibesys/state/`:

| Path | Agent confinement | Why |
| --- | --- | --- |
| `runs/` | hidden | The run's record: the core store (every role's turns, the judge's reviews, trusted measurements, controls and the adoption record), publications, artifacts, the roadmap and the objective document. Reading it breaks the barriers between roles and the trust boundary around evaluation; writing it could poison a resume. |
| `local/` | hidden | Machine-local state: credentials, caches and candidate worktrees. |
| `agent-visible/` | read-only (part of `.vibesys`) | Documents the framework writes for an agent to read by path. Today only the dynamic planner's handoff notes (`agent-visible/<run-id>/orchestrator-handoff/calls/`). |
| `project.json`, `.gitignore` | read-only | The project's identity and a fixed ignore contract; neither names a run. |

Everything an agent needs of the run reaches it through typed operations and prompts
(the roadmap, evidence, the history in the prompt), never through a path into `runs/`.
A role whose prompt names a path puts that document in `agent-visible/`, opened
through `Project.state.agent_visible_namespace`. The host environment gives the agent a
copy of the effective objective beside the run's logs, as a container gets it as a
mount, because the committed document lives in `runs/`.

`ProjectSandboxPaths.hidden_paths` is the set the policy hides, so adding a hidden state
directory is one entry there. The framework's own reads and writes of run state, the
isolation check (`GitTracker`), state commits and resume run outside the confinement and
see the real directories. Inside it, `git status` lists the hidden record as deleted:
the framework, not the agent, commits it.

A run that began before the handoff files moved kept them in `runs/`. On first use, the
handoff owner copies them to `agent-visible/`, so a resumed run lists them where its planner
can read them. For the first planning call after such a resume, the prompt still cites the
earlier paths (the run's record stores them), which the agent cannot read; the call's own
handoff then lists the new ones.

Reads are not uniformly hidden. Bubblewrap and Landlock deny all reads outside
the project and declared resources. Seatbelt allows broad reads and denies the
project's ancestor trees and all writes outside the project; use the default Docker run environment on
macOS when full read confinement matters.

## Known limits

- **Brokered Slurm work is not in a container.** The agent of `slurm` runs
  in Docker, but an agent GPU job (`[vibesys.agent_gpu]`) runs on a compute node,
  where Docker is normally unavailable, so the broker confines it with the host
  sandbox (`HostJobConfinement`; the broker refuses to start where that cannot
  be enforced). The job's `/tmp` is a private tmpfs, and `TMPDIR`, `TMP` and `TEMP` point at it, so scratch-space tools work and nothing outlives the job. The trusted gates run unconfined on the host, as they do in
  every other environment. The profiler server of `slurm` runs in the container
  with the image's `python3` and `pydantic`, importing the Slurm adapter from the
  read-only `libs` mount at its host path.
- **Metal on macOS runs on the host.** Docker on macOS cannot expose Metal/MPS,
  so a backend that declares itself host-only (`backend_is_host_only`, today
  Metal) runs its agent in the `host` environment on macOS: the host path
  `HostEnvironment` provides, under Seatbelt with enforcement required. The
  choice derives from the backend, never from a flag (`--docker` and
  `--run-environment local` stay rejected), and a log line states why. If
  Seatbelt is unavailable the run fails with `SandboxUnavailableError`; there
  is no unconfined fallback. The environment is recorded as `host`, and resume
  keeps it (only the retired `local` record migrates to Docker). Metal on any
  other platform still selects Docker and fails when its container starts.
- **Landlock** only adds rights, so it cannot carve a restriction out of the
  writable project. Read-only and hidden project paths are not enforced; each
  unenforced tier is logged at startup. Evaluator-input integrity is detected
  rather than prevented: the accuracy gate diffs protected paths against a
  trusted baseline and fails the round. Credentials in the project directory
  (`agent.toml`, `.env*`, `.vibesys/state/local`) and the run's record
  (`.vibesys/state/runs`) stay readable and writable: the unenforced hidden tier lists
  both at startup. A project under
  a tree Landlock must grant, such as `/tmp`, is refused.
- **Seatbelt** reads are broader than bubblewrap (above).
- **Hidden-path design.** The planning issue for prebuilt agent images (#674,
  closed) proposed "no explicit hidden access: deny what is not listed". The
  implementation kept project-level hidden paths and lowers them to mask mounts
  in containers, as described above. This page and
  [CLI flags](../cli-flags.md#runtime-environment) describe the implementation.
- **Candidate builds run under agent confinement, not as trusted commands.**
  The build stage executes the task's `[build]` argv through the candidate
  checkout's agent confinement (`WorkspaceResource.agent_sandbox()`; the
  candidate's `DockerSandbox` in the Docker run environment), never through the
  trusted command runner, so it gets the read-only and hidden project paths and
  none of the trusted evaluator environment (the prepared Go module cache and
  its offline policy). It fails closed where the run environment has no such
  confinement (the host environment) and does not run on the Slurm executor yet
  (#2968). Evaluator-owned files are checked before and after the build, as for
  the check. The outputs it produces are candidate bytes: the runtime only
  guarantees that check and measure load exactly what the measured commit's
  build produced (verified by path, regular-file type and digest), which the
  candidate controls anyway, since the evaluator loads candidate code.
- **Network** is open in every mode so the agent can reach its model provider.
