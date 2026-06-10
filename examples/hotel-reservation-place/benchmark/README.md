Benchmark for the hotel reservation system.

Metrics used:

  - reservations_per_sec 
  - overbooking_violations (must be 0)
  - success_count
  - error_count
  - p50_ms / p95_ms / p99_ms (latency specifications)
  - throughput_rps
  - cpu_percent
  - memory_mb


Load Level Specifications

| Level | Clients | Reservations |
|-------|---------|--------------|
| light | 5 | 50 |
| medium | 20 | 200 |
| heavy | 50 | 500 |

To run, start the server and then do:

```bash
pip install httpx psutil
python benchmark.py --load-level light
python benchmark.py --load-level medium
python benchmark.py --load-level heavy
```