Optimize the queue implementation in `main.py` for a multi-producer, single-consumer bounded FIFO queue.

Preserve the required interface:
- `VibeServeQueue(scenario="mpsc", capacity=...)`
- `enqueue(item) -> bool`
- `dequeue() -> item | None`
- `size() -> int`

The queue must remain linearizable, must not fabricate or duplicate items, and must respect capacity. Maximize CPU throughput for the MPSC workload.
