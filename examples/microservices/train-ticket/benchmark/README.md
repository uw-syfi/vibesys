# Train Ticket Workload

`workload.toml` defines a randomized stateful workload for the six mutable
Train Ticket v0.2.0 services. Its measured traffic mix is:

- 35% list operations;
- 25% point or secondary-index reads;
- 35% update followed by an exact validating read; and
- 5% create, read, delete, and negative-read operations.

The shared `servicebench` evaluator owns closed-loop saturation scheduling,
HTTP transport, logical-operation timing, aggregation, and structured results.
The Train Ticket application adapter owns only fixture construction and
response semantics.

From the repository root:

```bash
go -C examples/evaluators/microservice run ./cmd/servicebench \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --base-url http://localhost:8080 \
  --seed random \
  --output-json /tmp/train-ticket.json \
  --output-raw /tmp/train-ticket.ndjson
```

The configured concurrency is held busy for the measurement duration, so the
headline operations/sec value reflects achieved throughput instead of a fixed
offered-rate ceiling. The resolved random seed is written into the result and
can be replayed with `--seed <value>`. Override individual service targets with
repeated `--target name=address` flags.

One observation represents one logical operation and may contain multiple HTTP
invocations. Total latency starts at the scheduled arrival and ends after the
last response. Queue wait is reported separately, and the result reports both
logical-operation counts and physical HTTP invocation counts. Any transport,
schema, response-value, or read-your-write failure invalidates the run.
List operations validate every returned record's schema and require the exact
randomly selected runtime fixture to be present. Delete operations finish with
a negative point read, so no-op deletes are rejected.
