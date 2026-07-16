# KV-store Candidate Contract v1

This document is the normative interface between the trusted evaluators and an
untrusted candidate implementation. `OBJECTIVE.md` defines the optimization
goal; this file defines which implementations are valid.

## Required artifact and lifecycle

Candidates must provide an executable `run.sh` in the workspace root. The
evaluator invokes it as:

```text
./run.sh <port>
```

The launcher must keep the server in the launcher's process group, must not
daemonize, and must not escape into another session or process group. It may
`exec` a server or create a stable worker pool. The worker set must remain
stable during a scored interval so the evaluator can account for all server
CPU. On termination of the process group, all candidate processes must exit.

The server must listen on `127.0.0.1:<port>` and answer `PING` with `PONG`
within five seconds. It must remain available until terminated by the
evaluator.

## Wire protocol

The service speaks Redis RESP2 over TCP. It must:

- accept binary-safe RESP2 arrays of bulk-string command arguments;
- handle requests split across arbitrary TCP reads;
- handle multiple pipelined requests in one read and return replies in order;
- emit correctly typed RESP2 replies with valid lengths and CRLF framing;
- share one logical store across all connections and worker processes.

Command names are case-insensitive. Keys, field names, and values are arbitrary
byte strings.

## Required commands

The required data commands follow Redis semantics for the supported forms:

- `PING` returns `PONG`.
- `SET key value` stores a string and returns `OK`.
- `GET key` returns the string value or a null bulk string.
- `DEL key [key ...]` returns the number of existing keys removed.
- `HSET key field value [field value ...]` returns the number of newly created
  fields.
- `HMSET key field value [field value ...]` stores the fields and returns `OK`.
- `HGETALL key` returns all field/value pairs, or an empty array when absent.
- `DBSIZE` returns the number of top-level keys.
- `FLUSHDB` clears the store and returns `OK`.

Using a string command on a hash, or a hash command on a string, returns a
RESP2 `WRONGTYPE` error without changing the stored value. Invalid arity
returns a RESP2 error and does not mutate state.

The following compatibility commands are required because Redis clients may
issue them during connection setup:

- `COMMAND`, `CLIENT ...`, and `HELLO 2` must return a valid non-error RESP2
  reply accepted by the bundled clients.

Commands and options outside this subset are not required.

## Concurrency

Each command is atomic. Completed writes must be visible to later commands on
every connection. Concurrent operations must be linearizable: their effects
must be equivalent to some order consistent with non-overlapping calls.
Implementations must not maintain per-connection or per-process split-brain
copies of the store.

## Storage scope

The store is in-memory, non-persistent, single-node, and non-replicated.
Durability, authentication, clustering, eviction, and transactions are outside
this contract.
