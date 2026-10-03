# External boundaries

An external boundary is any call whose other side the code does not control:
an agent turn, an agent CLI, an MCP client, a cluster command (ssh, rsync,
sbatch, squeue, scancel), a remote evaluator, a subprocess. Each one can be
slow, fail, partly succeed, or return something unexpected. Handle that at the
owning library's implementation, once, so the core sees typed outcomes as
events. For lifecycle decisions, durable intent, and recovery, follow
[functional-core.md](functional-core.md).

## Agent output is untrusted input

- Parse it into a typed model at the boundary and reject unknown keys.
- Make invalid choices unrepresentable rather than rejected later. When the
  run does not offer an option (a workstream kind, a tool, a capability), the
  schema the agent sees omits it.
- Expect the same mistake after a correction. A bounded number of corrections
  ends in a typed error, never in a status that looks like success.
- Anything an agent names (ids, paths, parents) is checked against the state
  the host holds, not trusted.

## Deadlines

- Every call across a boundary has a deadline. Derive it from the limit of
  whoever waits on the result. For example, a tool an agent CLI calls returns
  before that CLI's tool-call timeout and says how to continue (a typed "still
  running" outcome with the next poll interval).
- State each limit once and derive the others from it. Two timeouts set
  independently on the two sides of one call will drift.

## Failure types

Classify failures where they happen, as a closed set the caller matches
exhaustively:

| Kind | Meaning | Caller action |
| --- | --- | --- |
| Transient | Retrying the same operation may succeed (connection reset, queue busy) | Bounded retry with backoff, if the operation is idempotent |
| Permanent | The request is wrong or the target refuses it | Surface a typed error to whoever chose the request |
| Unsupported | The executor cannot do this kind of work | Withdraw the capability for the rest of the run |
| Unknown | Acceptance or completion is ambiguous (timeout after send, killed process) | Reconcile against remote state before deciding; never default to success |

Do not turn an infrastructure failure into a verdict on the work (an
evaluator crash is not a failed candidate).

## Idempotence and retries

- Make operations safe to repeat: cancel by owner tag, not only by handle;
  create with a client-chosen id; write state atomically (temp file, then
  rename).
- Bound transport retries of transient failures inside the implementation,
  and retry only idempotent operations. Lifecycle retry decisions belong in
  the core and return new requests with durable intent; the shell adds none.

## Capabilities

What the system offers an agent or a planner is derived from what the
executor reports it supports, from one definition. A hard-coded list on
either side drifts. The first `unsupported` result withdraws the capability
and does not consume budget.

## Owned remote resources

A remote resource (a cluster job, a sandbox, a remote directory) is owned by
one scope in the process that created it. Tag it so it can be found again.
Physical cleanup passes through that scope on every exit. Stateful release
policy belongs in the core: record intent before cancellation or release,
reconcile tagged resources on restart, and retain ownership until termination
is confirmed or explicitly unresolved. See
[functional-core.md#durable-intent](functional-core.md#durable-intent).

## Agent tool servers

An MCP tool server is an external contract that agents call with arbitrary
arguments, in any order, at any time.

- Put each tool server in a compartmentalized home with a declared public
  interface and no imports from the orchestration that uses it. Valid homes
  are its own module, its own library under `libs/` when other packages
  reuse it, or a standalone server under `resources/`, such as the
  profilers in `resources/profilers/`.
- Servers in `src/` and `libs/` build their tools on the shared tool layer
  (`vs_agent` `ToolSpec` and `serve_stdio`), not a hand-built `FastMCP`, so
  that cross-cutting mechanisms cover every tool: deadlines, result size
  limits, fault injection. A standalone server under `resources/` may use
  FastMCP directly. Share its conventions through the `_common` package of
  its resource family, and test it the same way.
- Keep the server thin over a service with a typed API. The server parses
  arguments and formats replies. A stateful service rechecks every call through
  its pure core; its shell owns persistence and I/O.
- Derive which roles are offered a tool, and which calls the service
  authorizes, from one policy definition. That policy is combined with what
  the executor reports it supports.
