# vs-sandbox

Reusable host, Docker, and Modal sandbox backends for agent workspaces.

`vs-sandbox` owns the sandbox execution backends that do not depend on
VibeSys: container-backed workspaces implementing the `deepagents`
`BaseSandbox` protocol, host process confinement, plus Modal model-weight volume provisioning.
Applications wire these into their own run-environment policy.

## Concepts

- `DockerSandbox` runs agent operations in a local Docker container with
  host bind mounts, and cleans up tracked containers on exit or SIGINT.
- `HostResource` and related declaration types form a backend-neutral SDK for
  describing which host paths an application needs to import.
- `HostSandbox` and `SeatbeltSandbox` consume those declarations to confine a
  local process with bubblewrap on Linux or Seatbelt on macOS. Applications own
  their resource lists; this package owns validation and import mechanics.
- `ModalSandbox` mirrors `DockerSandbox` semantics on remote Modal GPUs,
  backing the workspace with an ephemeral Modal Volume that is synced at
  start and stop.
- `ensure_model_volume` provisions a per-model Modal Volume populated with
  HuggingFace model weights, reusing already-populated volumes.
