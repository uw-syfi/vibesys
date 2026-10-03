# vs-sandbox

## Responsibility

This package provides compute backend implementations, accelerator discovery
and monitoring, workspace execution backends, host resource and path policies,
lifecycle hooks, and Modal model-weight volume provisioning. Applications
select a compute stack and profiler policy, then declare the resources their
agents need.

## Concepts

- `Sandbox` is the command-execution protocol (`id`, `execute`) every sandbox
  kind satisfies; `SandboxExecutionResult` is its bounded result.
- `ComputeBackendImpl` constructs sandboxes for one compute stack. The public
  registry supplies CUDA, ROCm, Trainium, Metal, and CPU implementations;
  application code retains the policy for choosing among them.
- `AcceleratorDiscovery` and the GPU contention monitor isolate host hardware
  inspection from orchestration. Their deterministic Fakes are public through
  `vs_sandbox.api.testing`.
- `LocalShellSandbox` runs shell commands directly on the host with no
  isolation, for backends that have no container.
- `DockerSandbox` runs agent operations in a local Docker container with
  host bind mounts, and cleans up tracked containers on process exit (an interrupt reaches it through the interpreter's normal unwinding).
- `HostResource` and related declaration types form a backend-neutral SDK for
  describing which host paths an application needs to import. `agent_path`
  identifies the path the agent sees when a container remaps a host resource.
- `HostSandbox`, `LandlockSandbox`, and `SeatbeltSandbox` consume those
  declarations to confine a local process with bubblewrap, Landlock, or
  Seatbelt. Applications own their resource lists; this package owns
  validation and import mechanics. `WorkspaceSandbox` exposes a common path
  mapping and environment interface to callers.
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
- `vs_sandbox.api.skypilot` owns the optional SkyPilot cluster profile, CLI,
  evaluator bridge, wire protocol, and durable invocation-recovery mechanics.
  Applications select and compose that backend without carrying its
  implementation in product policy code.
- `vs_sandbox.api.evaluator_helpers` locates the installed Modal and SkyPilot
  helper programs that runtime composition mounts into remote sandboxes.
