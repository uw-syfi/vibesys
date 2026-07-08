Optimize the queue implementation in `main.py` for a lossy bounded queue.

Preserve the required interface:
- `VibeServeQueue(scenario="lossy", capacity=...)`
- `enqueue(item) -> bool`
- `dequeue() -> item | None`
- `size() -> int`

The queue may evict old items under pressure, but must not fabricate items and must never exceed capacity. Maximize CPU throughput for the lossy workload.
