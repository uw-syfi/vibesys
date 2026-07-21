# Shared application support

This directory contains application-specific wire and input-generation details
that benchmark and accuracy adapters must share so evaluator mode is not
observable. It must not contain response oracles, seed catalogs, endpoint
expectations, or correctness decisions. Those remain independently implemented
under `apps/` and `accuracyapps/`.
