# Objective - Linearizable queue input

Maximize throughput (operations/second) of a concurrent queue under the
workload defined by the active scenario, while satisfying correctness invariants.

Headline metric: `total_ops_per_sec` (maximize).

## Scenarios

| Scenario | Description |
|----------|-------------|
| spsc | Single-producer single-consumer bounded FIFO |
| mpmc | Multi-producer multi-consumer bounded FIFO |
| mpsc | Multi-producer single-consumer bounded FIFO |

## Candidate interface

- Provide a native shared library named `./queue-candidate.so`.
- Export the copying C ABI documented in
  `_evaluator/queue/QUEUE_ABI.md`.
- The candidate may use any language or combination of languages.
- The trusted runner supplies capacity, copied value size, and worker counts.
- No hardware accelerator is required; this workload is CPU-only.
- Start from the editable Rust implementation in `src/lib.rs`. It is an
  intentionally naive correctness baseline, not part of the trusted evaluator.
