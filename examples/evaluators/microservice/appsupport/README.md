# Shared application support

This directory contains application-specific wire, preflight, and
input-generation details that benchmark and accuracy adapters must share so
evaluator mode is not observable. It must not contain entity response oracles,
seed catalogs, or state-transition correctness decisions. Those remain
independently implemented under `apps/` and `accuracyapps/`.

Shared typed configuration validation, public request topology, and protocol
preflight expectations also belong here: malformed workload inputs, endpoint
versions, readiness traffic, and connection checks must not behave differently
by evaluator mode. Entity schemas and state-transition oracles remain separate.
