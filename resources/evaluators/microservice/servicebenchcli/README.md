# Servicebench CLI orchestration

This package owns reusable benchmark and accuracy command behavior: flag
parsing, workload resolution, lifecycle and signal handling, runner selection,
telemetry collection, and result output. `Run` receives an argument list, the
calling command's build version, and explicit `composition.Registration`
values, so task-owned commands can select
their correctness implementation without modifying the bundled servicebench
command.

The package owns no concrete application or driver imports. Those belong in
the executable composition root. Application semantics remain in benchmark or
accuracy adapters, and protocol behavior remains in drivers.
