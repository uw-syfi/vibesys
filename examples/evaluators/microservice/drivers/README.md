# Protocol drivers

This directory contains protocol-specific transports. A driver opens a client
for one named target; that client executes invocations and returns a common
`api.ProtocolResult` while preserving native status and metadata.

Drivers own connection/session behavior, wire serialization, timeouts exposed
by the transport, and transport-level error classification. They must not know
application operation names, prepare fixtures, validate application semantics,
schedule requests, or calculate metrics.

The current implementation is HTTP. Future gRPC or Thrift support should
implement the same `api.Driver` and `api.Client` interfaces, add focused driver
tests, and register the driver in the command without changing the engine.
