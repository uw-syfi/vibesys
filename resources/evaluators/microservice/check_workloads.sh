#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
train="$repo_root/examples/microservices/train-ticket/.vibesys/tasks/default/benchmark/workload.toml"
social="$repo_root/examples/microservices/repositories/deathstarbench/.vibesys/tasks/social-network-read-timeline/benchmark/workload.toml"
hotel="$repo_root/examples/microservices/repositories/deathstarbench/.vibesys/tasks/hotel-reservation/benchmark/workload.toml"
compose="$repo_root/examples/microservices/hotel-correctness/.vibesys/tasks/compose/benchmark/workload.toml"

go run ./cmd/servicebench --workload "$train" --validate-only
go run ./cmd/servicebench --mode accuracy --workload "$train" --validate-only
go run ./cmd/servicebench --workload "$train" --profile offered-load --validate-only
go run ./cmd/servicebench --workload "$social" --validate-only
go run ./cmd/servicebench --workload "$hotel" --telemetry-command-json '["go","run","./cmd/otelcapture","--input-json","/tmp/vibesys-hotel-traces.otlp.ndjson","--settle-seconds","5"]' --telemetry-output /tmp/vibesys-hotel-telemetry.json --telemetry-timeout 60 --validate-only
go run ./cmd/servicebench --mode accuracy --workload "$hotel" --validate-only
go run ./cmd/servicebench --workload "$compose" --telemetry-command-json '["go","run","./cmd/otelcapture","--input-json","/tmp/vibesys-hotel-traces.otlp.ndjson","--settle-seconds","5"]' --telemetry-output /tmp/vibesys-hotel-telemetry.json --telemetry-timeout 60 --validate-only
go -C "$repo_root/examples/microservices/hotel-correctness/.vibesys/tasks/compose/evaluator" run ./cmd/hotel-correctness --mode accuracy --workload "$compose" --validate-only
