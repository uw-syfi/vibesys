# Workload configuration

This package loads and validates the versioned TOML workload contract.

It provides:

- strict decoding that reports unknown fields;
- defaults for load and target behavior;
- named load and application-fixture profile overrides;
- structural and range validation for targets, operations, objectives, and
  constraints; and
- canonical JSON serialization used to identify the fully resolved workload.

Protocol- and application-specific validation stays with the selected driver or
application factory. Configuration errors should be reported before any load is
generated and should name the offending field or object.
