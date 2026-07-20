# Microservice Benchmark Evaluator

This trusted evaluator provides one load-generation and result contract for
online-application scenarios. Scenario bundles select an application adapter
and describe targets, operations, load, objectives, and constraints in a strict
TOML workload.

The first implementation includes a production HTTP driver and migrations for
Train Ticket and DeathStarBench Social Network. `api.Driver` is the extension
boundary for future gRPC and Thrift drivers; those wire implementations are not
part of this initial slice.

Run tests:

```bash
go test -race ./...
```

Run a workload from the repository root:

```bash
go -C examples/evaluators/microservice run ./cmd/microbench \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --base-url http://localhost:8080 \
  --output-json /tmp/result.json \
  --output-raw /tmp/requests.ndjson
```

See `DESIGN.md` for ownership boundaries and measurement semantics.
