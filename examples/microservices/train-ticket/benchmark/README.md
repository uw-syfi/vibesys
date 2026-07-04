# Train Ticket Benchmark

Runs a read-only HTTP workload against a Train Ticket deployment and reports
throughput, latency percentiles, and error rate.

```bash
python benchmark.py --base-url http://localhost:8080 --rate 20 --duration 30 --output-json /tmp/train_ticket_bench.json
```

The headline metric is `requests_per_second`.
