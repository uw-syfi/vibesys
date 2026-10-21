# Host gateway

`vibesysd` serves the generated HostService gRPC API over a Unix socket. It reads
published run logs and has no product policy or dependency on Python. It can
serve multiple clients from one process; a blocked Tail stream retains its own
bounded batch and uses HTTP/2 flow control.

The existing Python host helper and per-run byte relay remain in use by the
current desktop. This binary supplies the Hello and Tail transport for the new
client integration.

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
notifications wake readers; a three-second re-check guards against lost notifications.
Cancellation closes that stream's watcher and file descriptor.

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
