Optimize the queue implementation in `main.py` for a batched single-producer, single-consumer bounded queue.

Preserve the required interface:
- `VibeServeQueue(scenario="batch", capacity=...)`
- `enqueue(item) -> bool`
- `dequeue() -> list`
- `size() -> int`

Batch dequeues must not fabricate or duplicate items and must respect capacity. Maximize CPU throughput for the batch workload.
