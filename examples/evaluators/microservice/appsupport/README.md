# Shared application support

This directory contains application-specific wire and input-generation details
that benchmark and accuracy adapters must share so evaluator mode is not
observable. It must not contain response oracles, seed catalogs, endpoint
expectations, or correctness decisions. Those remain independently implemented
under `apps/` and `accuracyapps/`.

Shared typed configuration validation and public request topology also belong
here: malformed workload inputs and endpoint versions must not behave
differently by evaluator mode. Response schemas, status expectations, and state
transition oracles remain separate.
