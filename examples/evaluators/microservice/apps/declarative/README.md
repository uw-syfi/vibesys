# Declarative application adapter

This package implements ordinary HTTP workloads without application-specific
Go code. Each operation supplies an HTTP request and response expectation in
the workload TOML.

The adapter:

- expands deterministic `${counter}` and `${random}` request variables;
- builds paths, queries, headers, forms, and raw bodies;
- validates allowed HTTP statuses, JSON parsing, text fragments, and optional
  application-status envelopes; and
- captures configured response headers as custom timings.

It intentionally has no fixture setup and rejects application-specific config.
Use a typed adapter instead when an application needs dynamic datasets,
multi-step setup, or richer schema validation.
