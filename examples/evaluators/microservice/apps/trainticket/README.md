# Train Ticket application adapter

This adapter owns the Train Ticket v0.2.0 benchmark contract. It creates a
randomized connected fixture through public HTTP endpoints and builds strict
list, read, update/read, and create/read/delete operation plans.

The adapter does not issue measured requests or calculate metrics. It returns
plans to the shared engine, which executes and accounts for every invocation.
Per-record leases prevent concurrent updates from corrupting the evaluator's
expected state and are released through `FinishOperation` on every execution
path.

All fixture state is removed through public HTTP APIs after each trial. No
candidate database, process layout, persistence format, or implementation
language is assumed.
