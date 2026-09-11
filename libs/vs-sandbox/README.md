# vs-sandbox

Reusable host and Docker sandbox backends for agent workspaces, plus Modal
model-weight volume provisioning.

This is an internal import package shipped by the `vibesys` distribution. It
is not published as a separate Python distribution.

`vs-sandbox` owns the sandbox execution backends that do not depend on
VibeSys: container-backed workspaces implementing the `deepagents`
`BaseSandbox` protocol, host process confinement, plus Modal model-weight volume provisioning.
Applications wire these into their own run-environment policy.

## Concepts

- `DockerSandbox` runs agent operations in a local Docker container with
  host bind mounts, and cleans up tracked containers on exit or SIGINT.
- `HostResource` and related declaration types form a backend-neutral SDK for
  describing which host paths an application needs to import. `agent_path`
  names the path the confined process should see when it differs from the
  host path; leave it unset unless a resource is presented at a fixed
  container path. Host backends cannot remap, so `build_host_sandbox` rejects
  a resource whose `agent_path` disagrees with its host path.
- `HostSandbox`, `LandlockSandbox`, and `SeatbeltSandbox` consume those
  declarations to confine a local process with bubblewrap, Landlock, or
  Seatbelt. Applications own their resource lists; this package owns
  validation and import mechanics. Every `WorkspaceSandbox` exposes
  `agent_path(host_path)` (identity on host backends) and an `env` property
  (the environment the confined process runs with, guaranteed to carry HOME
  and PATH) so callers do not need backend-specific branches to answer either
  question.
- `ProjectPathPolicy` protects workspace-relative files and directories inside
  an otherwise writable project. It supports read-only paths and hidden paths,
  validates containment and overlap, and can require the host backend to fail
  closed when confinement is unavailable.
- `SandboxLifecycleHooks` lets trusted application code prepare an
  execution-capable sandbox in `before_ready`. Hooks run in registration
  order before startup completes, and rerun whenever a backend
  creates a replacement execution environment. Hooks must be idempotent;
  a raised exception aborts startup and triggers backend-owned cleanup.
- `ensure_model_volume` provisions a per-model Modal Volume populated with
  HuggingFace model weights, reusing already-populated volumes.
