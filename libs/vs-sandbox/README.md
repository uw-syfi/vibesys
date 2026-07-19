# vs-sandbox

Reusable Docker and Modal sandbox backends for agent workspaces.

`vs-sandbox` owns the sandbox execution backends that do not depend on
VibeSys: container-backed workspaces implementing the `deepagents`
`BaseSandbox` protocol, plus Modal model-weight volume provisioning.
Applications wire these into their own run-environment policy.

## Concepts

- `DockerSandbox` runs agent operations in a local Docker container with
  host bind mounts, and cleans up tracked containers on exit or SIGINT.
- `ModalSandbox` mirrors `DockerSandbox` semantics on remote Modal GPUs,
  backing the workspace with an ephemeral Modal Volume that is synced at
  start and stop.
- `ensure_model_volume` provisions a per-model Modal Volume populated with
  HuggingFace model weights, reusing already-populated volumes.
