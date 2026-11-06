#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
train="$repo_root/examples/microservices/repositories/train-ticket/.vibesys/tasks/default/benchmark/workload.toml"

go run ./cmd/servicebench --workload "$train" --validate-only
go run ./cmd/servicebench --mode accuracy --workload "$train" --validate-only
go run ./cmd/servicebench --workload "$train" --profile offered-load --validate-only
