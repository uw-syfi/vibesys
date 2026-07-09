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
- Accept the protocol v1 arguments and inherited socket descriptors documented in
  `_input_libs/queue-input-core/QUEUE_PROTOCOL.md`.
- The candidate may use any language or combination of languages.
- Trusted launcher arguments select the scenario, capacity, and client count.
- No hardware accelerator is required; this workload is CPU-only.
