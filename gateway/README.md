# Host gateway

`vibesys-host` serves the generated HostService gRPC API over a Unix socket. It reads
published run logs and has no product policy or dependency on Python. It can
serve multiple clients from one process; a blocked Tail stream retains its own
bounded batch and uses HTTP/2 flow control.

The Effect desktop app talks to this binary and nothing else on the host. It
supplies Hello, ListChatSessions, Tail, Skeleton, GetBlobs, TailStream and
Subscribe, and the orchestrator service below. It never starts a Python
process: the host RPCs that ran `vibesys host` commands (HostCall, Read, Watch
and Mutate, including InspectCandidate) were removed from the API.

## Run

From this directory, with Go 1.25 installed:

```sh
go build -o /tmp/vibesys-host ./cmd/vibesys-host
/tmp/vibesys-host serve
```

Both commands accept `--root PATH`, the directory a host serves from. `gatewayroot.Rule` states how a
root is chosen, and the [orchestrator README](../orchestrator/README.md#the-hosts-root) explains it. `serve --idle-timeout 10m` sets the period
with no open transport connections before shutdown. Open connections, even
without active RPCs, keep it alive, and keepalive (a ping after 30 s of
silence, then 10 s for its acknowledgement) ends one that has gone half-open.

`connect` dials the same socket and pipes stdin and stdout unchanged. If nobody serves it, it starts
the same executable with `serve` in a detached session and reads how that went from a
`HostStartReport` on a pipe: serving, refused with the reason, or another process got there first. It
never reads a log or waits for a socket to appear, and `--startup-timeout 10s` bounds the whole start.
The gateway inherits no SSH streams. Diagnostics go to stderr; stdout carries only transport bytes.
`connect` also replaces an idle host of another API hash, and leaves one with live runs alone, saying
so on stderr; the handshake then fails naming both hashes. A host started by `connect` stays up when
it cannot serve the orchestrator, because a desktop app still reads logs through it, and it says what
is wrong when asked.

A root has one socket, whatever the API hash: `ROOT/host.sock`, with `ROOT/host.lock` serializing
serving and stale-socket recovery, and the host's own log in `ROOT/host.log`. The socket path is a
function of the root with its symlinks resolved, so a starter and a finder that reach the same root
different ways get the same path. A socket address has one limit here, 100 bytes
(`lifecycle.MaxSocketPath`), below the 104 of macOS and the 108 of Linux. A longer `ROOT/host.sock`
is replaced by `/tmp/vibesys-UID/HASH.sock`, in a directory that must be owned by the user with no
group or other access, where HASH is a digest of the root. The socket is mode 0600.

The first 16 hexadecimal characters of the generated API hash select only where a provisioned binary
lives, `ROOT/gateway/PREFIX/vibesys-host`. `lifecycle.HashPrefixLength` defines that length once.
Hello compares the full hash, so a prefix collision cannot establish compatibility.

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
read extent, picks the latest keyframe at or below the boundary whose wire form
fits, and forwards its encoded bytes plus later records within a bounded unary
response. The API reports next_seq and complete; clients continue with
Tail(from_seq = next_seq). The wire form names each epoch's descriptors by the
SHA-256 of their bytes (EpochVersion.descriptors_sha256) and sends each distinct
set once in descriptor_sets, unless the request lists it in
known_descriptors_sha256; every other byte of the stored keyframe, including
fields the gateway does not know, is forwarded unchanged. A keyframe whose wire
form exceeds the response budget is skipped for the next older one (at most
four are tried); with none left, replay starts from the log. A phase 1 run returns no keyframe and a bounded prefix of its complete
records. Its committed_seq is the last included sequence, since it has no
published boundary. An incomplete committed prefix is DATA_LOSS when reached.

Keyframe headers record through_seq and the next log byte offset. Tail uses a
keyframe before the requested sequence, or a sparse in-memory index populated
while logs are read. Subsequent indexed seeks replay at most 127 preceding
records. File replacement or truncation invalidates cached positions. The index
is process-local and never writes into a run directory.

The runs topic (Subscribe) sends every linked run keyed by run_id, a synced frame, then replacements
and removals. Its source watches the index and run directories so
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

The build script emits `linux-amd64/vibesys-host`, `linux-arm64/vibesys-host`, and
`darwin-arm64/vibesys-host` under the supplied directory with `CGO_ENABLED=0`.
No target C toolchain is needed.

The Go check group checks formatting, runs vet, and runs package tests. Pure
property tests cover frame boundaries, corruption, and idle transitions. The
filesystem, Unix-socket, and process integration tests use temporary roots and
explicit handshakes. Timeouts only guard hangs; tests contain no sleeps.

## Package boundaries

See [Host composition](../docs/contributing/architecture.md#host-composition) for the boundary rule.

- `cmd/vibesys-host` exits with the result of `internal/hostmain.Main`.
- `internal/hostmain` wires CLI options, process signals, standard streams and typed services.
- `cmd/vibesys-client` exits with the result of `internal/clientmain.Main`, which gives the verbs in
  `orchestrator/clientcli` the real terminal, signals, clock and working directory, and finds the
  `vibesys-host` beside the client (symlinks followed, else on `PATH`) to start when none serves. It
  links neither the daemon nor the store; a test holds that.
- `internal/archcheck` checks each command's shape and its composition package's imports.
- `internal/runlog` owns framing and filesystem following.
- `internal/runindex` resolves published run symlinks.
- `internal/server` implements the HostService reads (Hello, Tail, Skeleton, GetBlobs, TailStream,
  ListChatSessions, Subscribe) and owns gRPC serving.
- `internal/lifecycle` owns socket publication, locks, and connection idleness.
- `internal/connect` owns detached startup and socket readiness.
- `internal/pipe` owns bidirectional copy and cancellation cleanup.
- `internal/terminal` owns the real terminal behind the keyed `run attach` view: single-key mode and its
  exact restore, the size, and resize signals. It decides nothing; the client composes it into `Env.Screen`.

Go's `internal` visibility and import-cycle checks enforce the module's private
surface and one-way dependencies. No existing Python or TypeScript module
imports this binary.

## The orchestrator

`serve` also hosts the orchestrator (`orchestrator/hosted`, through a `replace` of the sibling
module): its operator service shares this socket, and `mcp`, `run` and `send` are subcommands. See
[the orchestrator README](../orchestrator/README.md). `serve` writes a `HostLock` record into the lock it
holds and serves `HostControlService` beside the others, so `vibesys-client host status|stop|restart`
need no process list. `e2e/` runs it end to end with real processes
and a scripted provider: `go test -tags e2e ./e2e -v` (needs `uv`). `TestTheDesktopPathRunsAnOptimizationEndToEnd`
runs a lead, two workers and a judge on a toy task with the real `vibesys-cli`; CI runs it as the
`desktop-path` job.
