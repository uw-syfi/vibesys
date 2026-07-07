# CLI Flags and Supported Combinations

This document is the canonical map for VibeServe's CLI flag axes. Update it in
the same PR whenever a flag, backend, domain, loop, runtime environment, or
profiler behavior changes.

## Mental Model

Several flags look independent, but they combine into one execution contract:

| Axis | Flag | Meaning |
| --- | --- | --- |
| Search loop | `--outer-loop` | Which outer-loop policy runs: `agent`, `plain`, `evolve`, or `openevolve`. |
| Evaluation interface | `--interface` | Agent loop only. Whether the checker imports Python in-process or probes a service over the wire. |
| Compute backend | `--backend` | Hardware/runtime target: `cuda`, `metal`, `trainium`, or `cpu`. |
| Runtime environment | `--docker`, `--modal` | Where agent commands execute: local shell, Docker container, or Modal-backed workflow. |
| Profiler | `--profiler` | Bottleneck evidence source: `nsys`, `torch`, `neuron`, or `auto`. |
| Domain | `--domain` | Agent-loop problem-space prompt pack, such as `llm-serving` or `generic`. |
| Modality | `--modality` | Per-task I/O contract, such as `text_generation` or `speech_to_text`. |
| Target inputs | `--ref`, `--acc-checker`, `--bench` | Reference implementation, correctness checker, and benchmark harness for the target. |

Do not treat these as simple toggles. Some combinations imply a language,
startup contract, profiler, or sandbox capability.

## Outer Loops

| Value | Behavior | Notes |
| --- | --- | --- |
| `agent` | Orchestrator-driven loop with implementer, judge, and profiler roles. | Default. Supports `--domain`, `--interface`, and `--inner-loop`. |
| `plain` | Issue-board loop with deterministic issue draining and perf evaluation. | Uses backend prompt fragments from `src/vibe_serve/templates/_backend/`. |
| `evolve` | Evolutionary search over candidate implementations. | Uses mutator, judge, and profiler roles. |
| `openevolve` | MAP-Elites-style evolutionary loop. | Reuses evolve mutator, judge, and profiler prompts. |

Use `vibe-serve --outer-loop <kind> --help` for loop-specific flags.

## Interface

`--interface` applies to the agent loop.

| Value | Artifact contract | Language effect |
| --- | --- | --- |
| `inprocess` | Accuracy checker imports `main.py` directly. | Python is required. Prompts include the `uv` workflow and `VibeServeModel` contract. |
| `service` | Checker/benchmark exercise the artifact only through its network interface. | Implementation language is chosen by the agent. Checkers and benchmarks must be over-the-wire. |

`service` does not automatically rewrite a checker or benchmark. The target
inputs must already know how to probe the running service.

## Compute Backends

| Backend | Intended target | Sandbox support | Device handling | Default profiler behavior |
| --- | --- | --- | --- | --- |
| `cuda` | NVIDIA GPU serving systems. | Local, Docker, Modal. | Selects/reselects a GPU and can monitor contention. | Local/Docker use `nsys`; Modal uses `torch` when `--profiler auto`. |
| `metal` | Apple Silicon / MPS targets. | Local only. | No device selection or monitor. | Local `auto` resolves through the local runtime default. |
| `trainium` | AWS Trainium / NeuronCore targets. | Local and Docker; Modal unsupported. | Forwards `/dev/neuron*` in Docker; no per-device selection. | `auto` resolves to `neuron`. |
| `cpu` | CPU-only service/data-structure targets. | Local only in the current merged code. | No device selection or monitor. | Current agent/evolve `auto` behavior follows runtime defaults; the plain loop has CPU-specific prompt fragments. |

When a backend rejects a runtime environment, it should fail before agent work
starts with an actionable error.

## Runtime Environment

| Flags | Environment | Notes |
| --- | --- | --- |
| neither `--docker` nor `--modal` | Local shell. | Commands run on the host through `LocalShellBackend`. |
| `--docker` | Docker container. | Mounts the workspace and target inputs. Backend controls GPU/device passthrough. |
| `--modal` | Modal workflow. | Mutually exclusive with `--docker`. Intended for remote GPU dispatch. |

`--docker-image` overrides the backend's default container image when Docker or
Modal is active.

## Profiler

| Value | Intended use |
| --- | --- |
| `auto` | Let the runtime/backend pick the default profiler. |
| `nsys` | NVIDIA Nsight Systems. Requires a CUDA/NVIDIA profiling environment. |
| `torch` | PyTorch profiler. Used for in-process Python profiling and Modal GPU dispatch. |
| `neuron` | AWS Neuron profiler for Trainium. |

`--modal --profiler nsys` is rejected by the CLI because Modal runs must use the
torch profiler path.

Profiler prompts must match the interface and backend. For example, a service
interface must not assume `uv run python main.py` unless the implementation is
actually Python, and a CPU backend must not receive a GPU-kernel workflow.

## Domain and Modality

`--domain` supplies cross-cutting problem-space context for the agent loop.
Built-ins include:

| Domain | Meaning |
| --- | --- |
| `llm-serving` | Default LLM-serving guidance, including serving-system skills and judge gates. |
| `generic` | No extra domain guidance. Useful for custom/non-LLM targets. |
| path to `.md` | Custom domain pack. |

`--modality` supplies the task I/O contract, such as text generation or
speech-to-text. Domains should avoid hardcoding modality or interface
requirements that are already expressed by `--modality` or `--interface`.

## Common Commands

Default agent loop on local CUDA-compatible host:

```bash
vibe-serve \
  --outer-loop agent \
  --backend cuda \
  --interface inprocess \
  --ref examples/model-serving/Llama-3-8B/reference \
  --acc-checker examples/model-serving/Llama-3-8B/accuracy_checker \
  --bench examples/model-serving/Llama-3-8B/benchmark
```

Docker CUDA run:

```bash
vibe-serve --outer-loop agent --backend cuda --docker ...
```

Modal GPU run:

```bash
vibe-serve --outer-loop agent --backend cuda --modal --profiler torch ...
```

Trainium run:

```bash
vibe-serve --outer-loop agent --backend trainium --profiler auto ...
```

Over-the-wire service target:

```bash
vibe-serve \
  --outer-loop agent \
  --interface service \
  --domain generic \
  --ref examples/<target>/reference \
  --acc-checker examples/<target>/accuracy_checker \
  --bench examples/<target>/benchmark
```

CPU-only target in the current merged code:

```bash
vibe-serve --outer-loop agent --backend cpu --interface service ...
```

Use local execution unless the CPU backend's Docker support is present in your
checkout.

## Maintenance Rule

When adding or changing a flag:

1. Update this document.
2. Add or update validation for unsupported combinations.
3. Add prompt-rendering tests for combinations that change generated
   instructions.
4. Keep README focused on quickstart guidance and link here for details.
