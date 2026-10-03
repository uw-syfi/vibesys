# External boundaries

An external boundary is any call whose other side the code does not control:
an agent turn, an agent CLI, an MCP client, a cluster command (ssh, rsync,
sbatch, squeue, scancel), a remote evaluator, a subprocess. Each one can be
slow, fail, partly succeed, or return something unexpected. Handle that at the
boundary module, once, so the code behind it sees typed outcomes.

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
| Lost | The outcome is unknown (timeout after send, killed process) | Reconcile against the remote state before deciding |

Do not turn an infrastructure failure into a verdict on the work (an
evaluator crash is not a failed candidate).

## Idempotence and retries

- Make operations safe to repeat: cancel by owner tag, not only by handle;
  create with a client-chosen id; write state atomically (temp file, then
  rename).
- Retry only transient failures of idempotent operations, a bounded number of
  times, inside the boundary module. Callers do not add their own retries.

## Capabilities

What the system offers an agent or a planner is derived from what the
executor reports it supports, from one definition. A hard-coded list on
either side drifts. The first `unsupported` result withdraws the capability
and does not consume budget.

## Owned remote resources

A remote resource (a cluster job, a sandbox, a remote directory) is owned by
one scope in the process that created it. Tag it so it can be found again.
Every exit releases it through that scope, and a sweep on startup or resume
releases tagged resources that a hard kill left behind.

## Agent tool servers

An MCP tool server is an external contract that agents call with arbitrary
arguments, in any order, at any time.

- Put each tool server in its own module, or its own library under `libs/`
  when other packages reuse it. It must have a declared public interface and
  no imports from the orchestration that uses it.
- Build its tools on the shared tool layer (`vs_agent` `ToolSpec` and
  `serve_stdio`), not a hand-built `FastMCP`, so that cross-cutting
  mechanisms cover every tool: deadlines, result size limits, fault
  injection.
- Keep the server a thin adapter over a service with a typed API. The service
  holds the state and rechecks every call. The server parses arguments and
  formats replies.
- Derive which roles are offered a tool, and which calls the service
  authorizes, from one policy definition. That policy is combined with what
  the executor reports it supports.
