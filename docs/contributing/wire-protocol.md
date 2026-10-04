# Transport wire contract

This document is the single owner of the framing and connection semantics that every transport
carrying the frontend protocol must reproduce. It exists because the browser port (#808) adds a
second transport, a WebSocket gateway (#811), beside the existing Unix domain socket
(`src/server/transport/unix_jsonl.py`), and the two are built by different people in different
languages. Anything a transport can get wrong independently is written down here so the two ends
cannot silently disagree.

## Scope

This contract is transport level, not payload level. It specifies how protocol messages are framed,
which connection carries which role, how errors and disconnects are expressed, and how liveness and
flow control behave. It does not specify the schema of the messages themselves: those are the
Pydantic models in `src/server/api/protocol.py`, generated into
`clients/backend-client/src/generated/`. A transport relays whole protocol messages opaquely. If the
payload schema changes (for example a version bump), this layer does not.

It also does not specify how the WebSocket gateway serves the browser bundle over plain HTTP. The
response headers, the Content-Security-Policy, and the token-free `/assets/*` route are gateway
serving decisions with no counterpart on the Unix transport, so they are owned by
[`web-development.md`](web-development.md) rather than being `WP-*` decision tokens here.

The authoritative message taxonomy is three unions in `src/server/api/protocol.py`:

| Direction | Union | Members |
| --- | --- | --- |
| client to server | `ProtocolRequest` | `command.*`, `query.*`, `subscribe` (16 types) |
| server to client, control | `Response` | one per request, correlated by `request_id` |
| server to client, stream | `ServerMessage` | `subscribed`, `event`, `event_batch`, `protocol_error` |

`PROTOCOL_VERSION` is `1`. Every model sets `extra="forbid"`, which is load bearing: a client probes
for an optional field by sending it and treating rejection as "this server does not have the field"
(see `WP-CAPABILITY-PROBE`).

## Framing and connection decisions

Each decision below carries a stable token. The conformance corpus (`tests/conformance/`, see
`tests/conformance/README.md`) tags every scenario with the tokens it exercises, and the corpus gate
(`clients/scripts/check_conformance_corpus.mjs`) fails if any token here is never exercised or a
scenario tags a token that does not appear here. So this document cannot drift from the wire without
a test failing.

### WP-GRANULARITY: one frame carries exactly one protocol message

One transport frame is exactly one serialized protocol message. On the Unix transport a frame is one
JSONL line; `_write_message` serializes one model and appends `\n`
(`unix_jsonl.py:_write_message`). On the WebSocket transport a frame is one WebSocket message.

Batching is not framing. `event_batch` is one protocol message that carries many events in its
`events` list. The batch is a payload construct (`EventBatchMessage` in `protocol.py`), produced by
the server coalescing a watermark-consistent snapshot (`unix_jsonl.py:_stream`), and it crosses the
wire as a single frame on either transport. A transport never splits or merges frames.

### WP-FRAME-TYPE: text frames, UTF-8

Frames are UTF-8 text. On the Unix transport the bytes are UTF-8 encoded JSON. On the WebSocket
transport, frames are WebSocket text frames (opcode 0x1), not binary frames. Binary framing is
reserved for a future payload change and is out of scope here.

### WP-NEWLINE: the trailing newline is transport local

The `\n` the Unix transport appends is a framing delimiter for a byte stream, not part of the
message. A message-framed transport does not carry it. One WebSocket text frame is one message with
no trailing newline. A reader that strips a trailing newline before parsing is correct on both
transports; a reader that requires one is Unix specific and wrong.

### WP-FRAMER: NewlineFramer is Unix only

`NewlineFramer` (`clients/backend-client/src/newline-framer.ts`) reassembles messages from a byte
stream that may split a message across reads or pack several into one read. That problem exists only
on a stream transport. WebSocket delivers whole messages, so the framer does not run on the
WebSocket path. Framing lives in the transport adapter, below the shared client, so each transport
brings its own.

### WP-ROLES: one connection per role

The client opens three kinds of connection, and each maps to its own transport connection. This
mirrors the three sockets the Unix client opens today and needs no multiplexing.

| Role | Purpose | Lifetime |
| --- | --- | --- |
| control | requests and their responses (`command.*`, most `query.*`) | long lived; carries many sequential request/response pairs |
| subscribe | one `subscribe` then the event stream | one subscription; server streams until the peer goes away |
| chat | one `query.chat` on its own connection | one long-running request and its single response |

On the Unix transport the control connection reads requests in a loop and writes one response per
request (`unix_jsonl.py:_RequestHandler.handle`); a `subscribe` request takes over its connection
and never returns to that loop (`unix_jsonl.py:_stream`). A transport must preserve this: a
subscription owns its connection for the connection's life, and chat is isolated on its own
connection so a long agent turn does not block control traffic behind it.

### WP-SUBSCRIBE-ACK: subscribe is acknowledged before any batch, even on failure

The server answers an accepted `subscribe` with a `subscribed` message carrying `run_id` and
`latest_sequence` before it sends any `event_batch` (`unix_jsonl.py:_stream`). If the bootstrap
replay then fails, the server has already sent `subscribed` and reports the failure as a
`protocol_error` frame (`unix_jsonl.py:_write_stream_error`). So a client always sees `subscribed`
first on a dial the server accepted, and a bootstrap failure is a stream error after the ack, never
a rejected dial. A transport must not reorder or drop the ack.

The acknowledged identity and the `run_id` stamped on snapshots and events name the same run.
Projection state latches its first non-empty stamped identity and rejects later data for another
run. Empty or absent ids are legacy unknown values, not a request to replace an established owner.

### WP-PROTOCOL-ERROR: a protocol error is an in-band frame then close

`ProtocolErrorMessage` is a normal framed message (`type: "protocol_error"`). The server writes it in
band and then closes the connection (`unix_jsonl.py:_write_stream_error`). It is not expressed as a
transport close code. This is not cosmetic: the client suppresses its own disconnect callback after
it reads a `protocol_error`, because the drop that follows is the expected consequence of the error,
not a separate outage. A WebSocket transport that mapped a protocol error onto a close code instead
of an in-band text frame would change error semantics with no compile-time signal, so it must send
the frame and then close.

### WP-DISCONNECT: disconnect detection is transport specific and needs a liveness signal

The Unix server notices a gone client with a non-blocking `recv(1, MSG_PEEK | MSG_DONTWAIT)` that
returns empty on FIN (`unix_jsonl.py:_client_disconnected`), checked only while the stream is idle
(`unix_jsonl.py:_stream`). That probe has no WebSocket equivalent, and a WebSocket that dies without
a close (a slept laptop, a killed tab) leaves the server with no FIN to read. The disposition:

- A clean browser close (tab closed, navigation) sends a WebSocket close frame, which the gateway
  treats exactly as the Unix FIN: the subscriber count falls and teardown proceeds.
- A hard-dead peer (network partition, power loss) is reaped by a server-initiated WebSocket ping
  whose pong never arrives. Browsers answer a ping automatically below the JavaScript layer, so this
  needs no application protocol.
- A frozen or throttled tab still answers pings while reading nothing, so ping liveness cannot see
  it. That case is handled by the send-side write deadline below, and by the optional client
  heartbeat (`WP-HEARTBEAT`).

The bounds are stated, not inherited. The `WebSocketLimits` dataclass in `websocket.py` names every
one of them and `_serve_until_stopped` passes them all to `serve()`, so a `websockets` upgrade
cannot move a bound a subscriber depends on. Three of them compose into the liveness ceiling:

| Bound | Value | Role |
| --- | --- | --- |
| `ping_interval_seconds` | 20s | Idle period before the server probes the peer |
| `ping_timeout_seconds` | 20s | How long a pong may be outstanding before the peer fails |
| `close_timeout_seconds` | 10s | How long the server waits for the peer to echo its close frame |

A peer that stops answering is sent a close frame after 40s, and its socket is aborted at 50s when
that close is never echoed. Only the abort moves the connection to `CLOSED`, which is what the
stream loop polls for, so the subscription is released about 50.1s after the peer went silent (one
`_DISCONNECT_POLL_SECONDS`), and a non-detached run may then finish after
`RECONNECT_SETTLE_SECONDS`. Those are nominal bounds derived from the three values in the table, not
three independently measured constants. A peer that sends a FIN without a close frame needs none of
this and is released in 0.15s (measured).

That chain is not unconditional. `websockets` writes its own keepalive ping through the same
transport and outside the gateway's `_send`, so no write deadline covers it, and it can only go out
while the transport is below its low-water mark. It always is in practice, because a `_send` that
returns has drained below the mark and one that does not has aborted the transport, but the bound
above is a consequence of the send-side deadline holding rather than independent of it.

Flow control has two separate bounds in opposite directions, and conflating them is the easy
mistake. `max_queue`, `(32, 8)`, bounds frames arriving *from* the peer. `write_limit`, 32 KiB, is
the send-side high-water mark, and it is the one that carries the contract below.

**The send-side overflow policy is to stall the producer, not to drop events and not to disconnect
on the first slow read.** Past the high-water mark the producing coroutine suspends in the library's
`drain()` until the peer catches up. This is deliberately the same shape as the Unix path, where
`_write_message` in `unix_jsonl.py` does a blocking `wfile.write` plus `flush`, and it is
what preserves burst batching on both transports: a stalled stream loop is not reading the journal,
so the next `subscription_checkpoint` coalesces the whole backlog into one `event_batch` instead of
one frame per event. Slow consumers get fewer, larger batches rather than lost events.

The stall is bounded. A single frame may stall for `write_deadline_seconds`, one full keepalive
reaping window (40s) by default; past that the peer is treated as gone and its socket is aborted.
That bound is not optional decoration. `websockets` ends *every* write in `drain()`, including its
own keepalive ping and close frames, so a peer that neither drains its socket nor answers pings
blocks the very keepalive that was supposed to reap it. Without the deadline such a peer was never
reaped at all, its subscription was never released, and a non-detached run never exited: measured,
still counted as active after 90s. The socket is aborted rather than closed because a close frame is
itself a write and would re-enter the same stalled `drain()`.

The Unix path has no equivalent deadline: its blocking write stalls the handler thread
indefinitely, which is pre-existing behavior this contract records rather than changes. Its
gone-client probe fires only while the stream is idle. So the two transports agree on the stall and
differ on the ceiling.

### WP-HEARTBEAT: the application heartbeat is optional and probe advertised

Because browsers do not expose WebSocket ping and pong to JavaScript, a client that wants to detect a
dead server (as opposed to the server detecting a dead client) needs an application-level signal. The
frame shape is reserved here so both ends agree before either implements it: an optional
`heartbeat_ms` field on `SubscribeRequest`, capability probed like `tail` and `store_id`
(`WP-CAPABILITY-PROBE`). A server that has the field emits a periodic keepalive frame the client can
time out against; a server that predates it rejects the field, and the client falls back to no
application heartbeat. This reservation only fixes where the frame rides so it is additive.

No server-side heartbeat frame exists, and the server does not need one. The direction this contract
governs, the server detecting a dead client, is served entirely by protocol-level pings, because a
browser's own WebSocket implementation answers them below the JavaScript API (`WP-DISCONNECT`). Only
the opposite direction, a client detecting a dead server, would use the reserved field, so it stays
reserved and unimplemented on both transports. `tests/conformance/scenarios/heartbeat-probe.json`
records the reservation: the field is rejected today, and that rejection is the capability probe
working, not a gap. It records rather than pins, because no runner executes it yet
(`tests/conformance/README.md`).

### WP-CAPABILITY-PROBE: capabilities are probed by rejection, not advertised

There is no capability advertisement. A client discovers whether the server supports an optional
subscribe field by sending it and reading the result: acceptance means the field works, a
`extra="forbid"` rejection means the server predates it. `tail` and `store_id` are the two fields
this already governs (`SubscribeRequest` in `protocol.py`; the client's probe-and-fallback in
`clients/backend-client/src/persistent-event-stream.ts`).

The recorded consequence, accepted deliberately: a browser resume that carries `store_id` against a
server that lacks it costs one extra speculative dial (the rejected probe, then the cursor-only
resume). Probe-by-rejection is kept rather than adding an advertisement frame, because the number of
optional fields is small and an advertisement is a new frame both transports would have to reproduce.
Revisit if the option set grows.

### WP-REBOOTSTRAP: a store swap or tail overflow restarts the replay with a non-contiguous sequence

Two conditions make the server abandon a client's cursor and send a fresh bootstrap batch instead of
a continuation:

- The run attached its durable event store after the client subscribed, so the batch's `store_id`
  differs from what the client folded (`unix_jsonl.py:_stream`). Sequences are only comparable within
  one store.
- More live output landed in one wait than the `tail` bound was willing to replay
  (`unix_jsonl.py:_stream`).

In both cases the next `event_batch` supersedes the client's fold rather than extending it, and its
`through_sequence` is not the client's cursor plus one. A client keys continuation on `store_id` and
the batch, not on sequence contiguity. A transport carries these batches unchanged; the rebootstrap
decision is server logic, not framing.

Rebootstrap is the sole projection transition allowed to adopt a different `run_id`. Within any
ordinary batch, event identity also guards the batch-level `active_executions`,
`through_sequence`, and `history_after_sequence`: if an event is rejected as foreign, those
metadata do not apply. A same-run rebootstrap may retain snapshot state that the replay tail cannot
reconstruct; a changed or unknown identity must start without state owned by the prior run.

### WP-IDENTITY: the issuing connection is identified by a stable client id

Two clients on one run is the configuration the browser port creates, and the server must be able to
say which connection issued a request (for #840's acknowledgment attribution, and for the two-client
conformance scenario). This is the optional protocol field `client_id`. A frontend transport
generates one stable id for its client instance and carries it on control requests, subscriptions,
and dedicated chat requests, including requests sent after a reconnect. `Response`,
`SubscribedMessage`, and `ProtocolErrorMessage` reflect it. An empty id is the backward-compatible
default for a peer that predates attribution.

The field is defined in `src/server/api/protocol.py` and generated into the TypeScript bindings. The
corpus reserves a two-client scenario that asserts independent attribution with one Unix and one
WebSocket subscriber live in one process.

## The conformance corpus

The framing decisions above are enforced by one shared fixture corpus at `tests/conformance/`, read
by both conformance suites: the client-side fold suite (#812) and the server-side scenario suite
(#811). Neither side keeps a private copy. The corpus has two parts:

- `tests/conformance/events/` holds one fixture per `EventType` (the event-kind enum in the generated
  schema). The corpus gate enumerates the enum from
  `clients/backend-client/src/generated/protocol.schema.json` and fails if a kind has no fixture, so
  a new event kind cannot land without a fixture.
- `tests/conformance/scenarios/` holds the connection scenarios (bootstrap, resume, rebootstrap,
  capability probes, protocol error, and so on), each tagging the framing decisions it exercises.

See `tests/conformance/README.md` for the layout and how to run the gate, and
`tests/conformance/FORMAT.md` for
the fixture and scenario formats. The replay runners that execute the scenarios against a live
transport land with the transports themselves (888b for the client fold runner over the Unix
transport, #811 for the two-transport scenario); this document and the corpus define what they must
reproduce.
