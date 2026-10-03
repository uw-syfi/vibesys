# vs-async-ops

Resource-neutral durable asynchronous operation lifecycle coordination. Domain
packages own request and result meaning; this library owns only scheduling,
serialization, cancellation, restart interruption, and bounded observation.

`INTERRUPTED` is terminal. After restart, callers resume domain work by
submitting a new operation that refers to their durable domain session.

`await_result` applies its finite bound to startup, durable reads, and
notification waiting. It returns the last observed nonterminal record on
deadline expiry, or raises `OperationObservationTimeoutError` when no record
was observable before the deadline. Timeout never cancels work.

`OperationPolicy.cancellation_timeout_s` optionally bounds runner cancellation
and task termination. Expiry still records the requested terminal state, then
raises `OperationCancellationTimeoutError` as a cleanup diagnostic. Lifecycle
event sinks are observers: their failures are reported through the injected
error sink and cannot strand accepted work.
