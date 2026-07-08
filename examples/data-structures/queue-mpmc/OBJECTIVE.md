Optimize the queue implementation in `main.py` for a multi-producer, multi-consumer bounded FIFO queue.

Preserve the required interface:
- `VibeServeQueue(scenario="mpmc", capacity=...)`
- `enqueue(item) -> bool`
- `dequeue() -> item | None`
- `size() -> int`

The queue must remain linearizable, must not fabricate or duplicate items, and must respect capacity. Maximize CPU throughput for the MPMC workload.
