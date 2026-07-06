# Train Ticket Benchmark

Runs a read-only, fixed-rate open-loop HTTP workload (weighted mix of the
station/train/trip/route/price/config list endpoints plus a welcome endpoint)
against a Train Ticket deployment and reports throughput, latency
distributions, and error rate.

```bash
python benchmark.py --base-url http://localhost:8080 --rate 20 --duration 30 --output-json /tmp/train_ticket_bench.json
```

## Metric semantics

- The benchmark schedules `rate * duration` requests at fixed intervals and
  executes them on a bounded worker pool (`--concurrency`).
- `latency_ms` (the primary latency distribution) is measured from each
  request's **scheduled send time**, so when the deployment cannot keep up
  and requests queue client-side, the queueing delay counts — the numbers do
  not flatline at per-request service time (no coordinated omission).
  `service_time_ms` is the HTTP exchange alone and `queue_wait_ms` shows the
  gap between the two; a large `queue_wait_ms` means the offered rate exceeds
  what the deployment sustains at this concurrency.
- Latency distributions cover successful requests only; always read them next
  to `error_rate` and `timeout_failures`.
- `requests_per_second` (the headline metric) is completed successful
  requests over total elapsed time (including the drain of in-flight requests
  after the last submission). At a fixed offered load it approximates
  `rate * (1 - error_rate)` and is capped by `--rate`; it only measures
  capacity when the deployment saturates below the target rate.
- If the client itself cannot submit at the target rate, a warning is printed
  and `offered_rate` records what was actually offered.
- Each request opens a fresh TCP connection (no keep-alive), so connection
  setup is included in latency; keep that in mind for remote targets.

An HTTP-200 response carrying a Train Ticket error envelope
(`{"status": 0, "msg": ...}`) counts as a failed request, as do transport
errors, timeouts, and unexpected response shapes. Individual request failures
never abort the run; they are recorded and summarized in `errors_by_type` and
`sample_errors`. Ctrl+C stops the run early and reports the completed portion
(exit code 130).

Exit code is 0 when `error_rate <= --max-error-rate` (default 0.0: any
failure makes the run fail).
