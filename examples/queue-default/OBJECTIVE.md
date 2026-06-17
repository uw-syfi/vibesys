# Objective - Queue default harness

Maximize throughput (operations/second) of a concurrent queue under the
workload defined by the active scenario, while satisfying correctness invariants.

## Scenarios

| Scenario | Description |
|----------|-------------|
| spsc | Single-producer single-consumer bounded FIFO |
| mpmc | Multi-producer multi-consumer bounded FIFO |
| mpsc | Multi-producer single-consumer bounded FIFO |
| lossy | Single-writer lossy overwrite (newest item wins on full) |
| batch | SPSC batched handoff (producer drains into consumer in bulk) |

## Notes

- Reference uses collections.deque with threading.Lock.
- Protocol: enqueue(item), dequeue(), size(), capacity.
- No hardware accelerator required; CPU-only.
