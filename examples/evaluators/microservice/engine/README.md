# Benchmark engine

This package owns protocol-neutral benchmark execution and result construction.
It depends on the interfaces in `api/` and resolves concrete extensions through
the registry.

Its responsibilities include:

- reset, prepare, warmup, measurement, and repetition lifecycle;
- fixed-rate open-loop arrival scheduling with bounded concurrency;
- scheduled, dispatched, sent, and completed timestamps;
- queue-wait, protocol-time, and total-latency observations;
- offered-rate, scheduler-lag, and queue-depth diagnostics;
- per-operation and per-trial distributions;
- success/error and load-sustainability constraints; and
- median, MAD, IQR, and bootstrap aggregation across trials.

The engine omits the trusted `primary_value` when any trial is invalid. It must
not branch on a protocol or application name; fake-driver tests protect this
extension boundary.
