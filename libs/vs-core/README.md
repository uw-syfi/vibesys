# vs-core

Pure lifecycle values and `step(state, event) -> Transition`. The async shell
calls `Strategy.decide(project(state))` separately and commits the envelope
before executing requests. Import only `vs_core.api`.

Wave 0 publishes strict immutable schemas, dispatch, capability validation,
operation codecs, request registration and revision ownership. Area reducers
raise `KernelNotImplemented` with their owning lane until wave 1 implements
them. No existing dynamic execution path is moved or activated here.
