# Objective - Linearizable queue input

Maximize throughput (operations/second) of a concurrent queue under the
workload defined by the active scenario, while satisfying correctness invariants.

## Scenarios

| Scenario | Description |
|----------|-------------|
| spsc | Single-producer single-consumer bounded FIFO |
| mpmc | Multi-producer multi-consumer bounded FIFO |
| mpsc | Multi-producer single-consumer bounded FIFO |

## Candidate interface

- Provide an executable `./queue-candidate` launcher.
- Accept `--vibeserve-queue-shm <path>` and serve protocol v1 from
  `_input_libs/queue-input-core/QUEUE_PROTOCOL.md`.
- The candidate may use any language or combination of languages.
- The trusted header selects the scenario, capacity, and client count.
- No hardware accelerator is required; this workload is CPU-only.
