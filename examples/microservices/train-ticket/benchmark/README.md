# Train Ticket Workload

`workload.toml` describes the application-specific portion of the benchmark:
the seven read endpoints, their weights, Train Ticket response-envelope checks,
the default load, and the `requests_per_second` objective.

The shared evaluator under `examples/evaluators/microservice` owns scheduling,
HTTP transport behavior, scheduled-arrival timing, aggregation, and structured
results.

From the repository root:

```bash
go -C examples/evaluators/microservice run ./cmd/microbench \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --base-url http://localhost:8080 \
  --output-json /tmp/train-ticket.json \
  --output-raw /tmp/train-ticket.ndjson
```

The workload preserves the previous fresh-connection policy explicitly with
`session_policy = "new_per_request"`. Override individual targets with repeated
`--target name=address` flags for the local direct-service deployment.

Latency starts at each scheduled arrival, so queue wait is included in total
latency and reported separately. A run is invalid when it cannot sustain at
least 95% of the requested offered rate or when any request fails.
