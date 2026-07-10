# Objective - Queue default input

Maximize throughput (operations/second) of a concurrent queue under the
workload defined by the active scenario, while satisfying correctness invariants.

## Scenarios

| Scenario | Description |
|----------|-------------|
| spsc | Single-producer single-consumer bounded FIFO |
| mpmc | Multi-producer multi-consumer bounded FIFO |
| mpsc | Multi-producer single-consumer bounded FIFO |

## Notes

- Reference uses collections.deque with threading.Lock.
- Protocol: enqueue(item), dequeue(), size(), capacity.
- No hardware accelerator required; CPU-only.
