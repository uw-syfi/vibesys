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
JSONL line; `_write_message` serializes one model and appends `\n` (`unix_jsonl.py:192-195`). On the
WebSocket transport a frame is one WebSocket message.

Batching is not framing. `event_batch` is one protocol message that carries many events in its
`events` list. The batch is a payload construct (`EventBatchMessage` in `protocol.py`), produced by
the server coalescing a watermark-consistent snapshot (`unix_jsonl.py:120-138`), and it crosses the
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
request (`unix_jsonl.py:47-72`); a `subscribe` request takes over its connection and never returns
to that loop (`unix_jsonl.py:55-63`). A transport must preserve this: a subscription owns its
connection for the connection's life, and chat is isolated on its own connection so a long agent
turn does not block control traffic behind it.

### WP-SUBSCRIBE-ACK: subscribe is acknowledged before any batch, even on failure

The server answers an accepted `subscribe` with a `subscribed` message carrying `run_id` and
`latest_sequence` before it sends any `event_batch` (`unix_jsonl.py:96-102`). If the bootstrap replay
then fails, the server has already sent `subscribed` and reports the failure as a `protocol_error`
frame (`unix_jsonl.py:80-95`). So a client always sees `subscribed` first on a dial the server
accepted, and a bootstrap failure is a stream error after the ack, never a rejected dial. A
transport must not reorder or drop the ack.

### WP-PROTOCOL-ERROR: a protocol error is an in-band frame then close

`ProtocolErrorMessage` is a normal framed message (`type: "protocol_error"`). The server writes it in
band and then closes the connection (`unix_jsonl.py:173-182`). It is not expressed as a transport
close code. This is not cosmetic: the client suppresses its own disconnect callback after it reads a
`protocol_error`, because the drop that follows is the expected consequence of the error, not a
separate outage. A WebSocket transport that mapped a protocol error onto a close code instead of an
in-band text frame would change error semantics with no compile-time signal, so it must send the
frame and then close.

### WP-DISCONNECT: disconnect detection is transport specific and needs a liveness signal

The Unix server notices a gone client with a non-blocking `recv(1, MSG_PEEK | MSG_DONTWAIT)` that
returns empty on FIN, checked only while the stream is idle (`unix_jsonl.py:104-108`, `:184-190`).
That probe has no WebSocket equivalent, and a WebSocket that dies without a close (a slept laptop, a
killed tab) leaves the server with no FIN to read. The disposition:

- A clean browser close (tab closed, navigation) sends a WebSocket close frame, which the gateway
  treats exactly as the Unix FIN: the subscriber count falls and teardown proceeds.
- A hard-dead peer (network partition, power loss) is reaped by a server-initiated WebSocket ping
  whose pong never arrives. Browsers answer a ping automatically below the JavaScript layer, so this
  needs no application protocol.
- A frozen or throttled tab still answers pings while reading nothing, so ping liveness cannot see
  it. That case is handled by a send-side write deadline on the gateway, not by this contract, and
  by the optional client heartbeat below.

The exact ping cadence, the write deadline, and the send-buffer overflow policy are connection-health
mechanics owned by #890. This contract reserves the client-visible surface: an optional application
heartbeat (`WP-HEARTBEAT`).

### WP-HEARTBEAT: the application heartbeat is optional and probe advertised

Because browsers do not expose WebSocket ping and pong to JavaScript, a client that wants to detect a
dead server (as opposed to the server detecting a dead client) needs an application-level signal. The
frame shape is reserved here so both ends agree before either implements it (#890): an optional
`heartbeat_ms` field on `SubscribeRequest`, capability probed like `tail` and `store_id`
(`WP-CAPABILITY-PROBE`). A server that has the field emits a periodic keepalive frame the client can
time out against; a server that predates it rejects the field, and the client falls back to no
application heartbeat. No heartbeat frame is defined until #890 lands; this reservation only fixes
where it rides so it is additive.

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
  differs from what the client folded (`unix_jsonl.py:121-128`). Sequences are only comparable within
  one store.
- More live output landed in one wait than the `tail` bound was willing to replay
  (`unix_jsonl.py:110-115`).

In both cases the next `event_batch` supersedes the client's fold rather than extending it, and its
`through_sequence` is not the client's cursor plus one. A client keys continuation on `store_id` and
the batch, not on sequence contiguity. A transport carries these batches unchanged; the rebootstrap
decision is server logic, not framing.

### WP-IDENTITY: the issuing connection is identified by a stable client id

Two clients on one run is the configuration the browser port creates, and the server must be able to
say which connection issued a request (for #840's acknowledgment attribution, and for the two-client
conformance scenario). This is a protocol field, `client_id`, carried on requests and reflected on
acknowledgments. It is additive and optional: a client that omits it keeps working, matching the
`extra="forbid"` probe pattern.

The field itself lands in `src/server/api/protocol.py` with regenerated bindings, which is outside
the client team's delegated-merge capability, so it is filed as the maintainer half of #888 (888c in
the port plan). This document specifies it; the corpus reserves a two-client scenario that asserts
independent attribution once the field and a second transport both exist (that scenario lands with
the gateway, #811, because it needs one Unix and one WebSocket subscriber live in one process).

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
