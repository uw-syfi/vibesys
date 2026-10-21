# Host gateway

`vibesysd` serves the generated HostService gRPC API over a Unix socket. It reads
published run logs and has no product policy or dependency on Python. It can
serve multiple clients from one process; a blocked Tail stream retains its own
bounded batch and uses HTTP/2 flow control.

The existing Python host helper and per-run byte relay remain in use by the
current desktop. This binary supplies Hello, Tail, Skeleton, WatchRuns,
GetBlobs and TailStream for the new client integration.

## Run

From this directory, with Go 1.25 installed:

```sh
go build -o /tmp/vibesysd ./cmd/vibesysd
/tmp/vibesysd serve
```

Both commands accept `--root PATH`, the host's `.vibesys` directory, which
normally defaults to `~/.vibesys`. `serve --idle-timeout 10m` sets the period
with no open transport connections before shutdown. Open connections, even
without active RPCs, keep it alive.

`connect` dials the same socket and pipes stdin and stdout unchanged. If needed,
it starts the same executable with `serve` in a detached session and waits up
to `--startup-timeout 10s` for the socket. The gateway inherits no SSH streams.
Diagnostics go to stderr; stdout carries only transport bytes.

The first 16 hexadecimal characters of the generated API hash select
`ROOT/gateway/PREFIX/sock`; the provisioned binary belongs at
`ROOT/gateway/PREFIX/vibesysd`. `lifecycle.HashPrefixLength` defines that length
once. This prefix selects the binary and directory only. Hello compares the
full hash, so a prefix collision cannot establish compatibility. Its directory
is mode 0700 and the socket is mode 0600. A process lock serializes serving and
stale-socket recovery for each prefix. Different prefixes can coexist. Startup
validates the complete socket path before binding or dialing and names the
path and platform limit on failure. The limit is 104 bytes on macOS and 108
on Linux, including the terminating NUL. A host with a longer root must
supply a shorter `--root`.

## Disk input and stream behavior

Runs publish `ROOT/runs/RUN_ID` as a symlink to a directory containing
`run.log`. The gateway resolves only this index; it does not discover projects.
The directory must exist, but Tail can start before `run.log` is created.

A log begins with `VSRLOG01`. Each record is an unsigned varint payload length,
a four-byte little-endian CRC32C (Castagnoli), and the encoded store Record.
The reader advances only over complete, checksum-verified records. Incomplete
tails wait for an append; a complete bad checksum ends Tail with `DATA_LOSS`.
Record payloads are capped at 16 MiB to bound allocation from corrupt lengths.
Batches target 1 MiB of raw payload; a larger record is sent alone. A stream
keeps at most its current batch and one lookahead record before a send.

The gateway reads the envelope sequence and forwards each original encoded
record unchanged, including unknown fields. Tail validates increasing sequence
numbers. Files must be append-only. A detected replacement or truncation ends
the stream instead of silently replaying a different log. File and directory
notifications wake readers, including an atomic run.head rename that advances
committed_seq. File contents are never polled. Cancellation closes that stream's
watcher and file descriptor.

When run.head has committed_seq, Tail and Skeleton stop at that sequence. A
present zero commits no records. With no head or no committed_seq, every complete
record is committed, preserving the phase 1 converter path. Skeleton freezes its
read extent, picks the latest keyframe at or below the boundary, and forwards its
encoded bytes plus later records within a bounded unary response. The API
reports next_seq and complete; clients continue with Tail(from_seq = next_seq).
A keyframe that exceeds the response budget is omitted and replay starts from
the log. A phase 1 run returns no keyframe and a bounded prefix of its complete
records. Its committed_seq is the last included sequence, since it has no
published boundary. An incomplete committed prefix is DATA_LOSS when reached.

Keyframe headers record through_seq and the next log byte offset. Tail uses a
keyframe before the requested sequence, or a sparse in-memory index populated
while logs are read. Subsequent indexed seeks replay at most 127 preceding
records. File replacement or truncation invalidates cached positions. The index
is process-local and never writes into a run directory.

WatchRuns sends every linked run, an initial_complete marker, then replacements
and removal tombstones by run_id. It watches the index and run directories so
atomic head replacement is visible. Liveness probes run.lock with a nonblocking
shared flock probe against the writer's exclusive lock and leaves the persisted head unchanged. A run with an ended lifecycle is
ended; otherwise a held lock means alive and an unheld lock means interrupted.
A phase 1 run without a head or lock has unknown liveness. Probes happen on
subscription and filesystem changes. Previously live runs are also re-probed
every five seconds, using the injected clock. This is the bounded exception to
file polling: flock unlock after a crash has no filesystem notification. Ended
and previously interrupted runs are not periodically probed.

GetBlobs returns a result for each requested hash, including failures, and
verifies SHA-256 before returning content. Paths must be lowercase hexadecimal
hashes; content paths are confined to the indexed run directory even through
symlinks. TailStream uses validated stream identifiers, reads from a byte offset,
and follows file notifications. Replacement or truncation fails with DATA_LOSS. A future offset waits for the
file to reach it; the call can subscribe before a stream exists.
Response limits and per-hash error semantics live in the
[API contract](../proto/vibesys/api/v1/host.proto).

## Build and verify

From the repository root:

```sh
./gateway/build.sh .agent-logs/gateway-binaries
./support/repoctl/repoctl run-checks --group gateway_checks
```

The build script emits `linux-amd64/vibesysd`, `linux-arm64/vibesysd`, and
`darwin-arm64/vibesysd` under the supplied directory with `CGO_ENABLED=0`.
No target C toolchain is needed.

The Go check group checks formatting, runs vet, and runs package tests. Pure
property tests cover frame boundaries, corruption, and idle transitions. The
filesystem, Unix-socket, and process integration tests use temporary roots and
explicit handshakes. Timeouts only guard hangs; tests contain no sleeps.

## Package boundaries

- `cmd/vibesysd` wires CLI options, process signals, and standard streams.
- `internal/runlog` owns framing and filesystem following.
- `internal/runindex` resolves published run symlinks.
- `internal/server` implements Hello and Tail and owns gRPC serving.
- `internal/lifecycle` owns socket publication, locks, and connection idleness.
- `internal/connect` owns detached startup and socket readiness.
- `internal/pipe` owns bidirectional copy and cancellation cleanup.

Go's `internal` visibility and import-cycle checks enforce the module's private
surface and one-way dependencies. No existing Python or TypeScript module
imports this binary.
