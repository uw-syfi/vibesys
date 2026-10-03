# Fault injection

Code that talks to agents, clusters, MCP clients, or subprocesses fails in ways
a canned reply never exercises. Test it with generated behavior and scheduled
faults, deterministically, and check invariants over the result.

## Inject at the boundary, once

- Inject faults through a wrapper around the boundary's interface (the agent
  client, the MCP tool dispatch, the cluster command runner, the evaluator
  executor). The wrapper implements the same interface and is generic over
  what crosses it: it does not know roles, tool names beyond a key, or job
  kinds.
- Do not add fault branches to production code, and do not write a separate
  faulty Fake per role, tool, or test. New roles, tools, and output fields
  are covered without new test code.
- With no faults scheduled, the wrapper is a pass-through, and the contract
  suite proves it.
- A Fake's own failure knobs (see
  [fakes-and-contracts.md](fakes-and-contracts.md)) remain for single
  scenario tests; fault schedules are for runs that combine many faults.

## Agent behavior

Generate replies from the output schema the turn declares, mixing:

- valid replies;
- plausible but wrong replies: unknown or reused ids, options the run does
  not offer, empty plans, the same mistake after a correction;
- malformed replies: invalid JSON, wrong types, extra keys;
- transport faults: crash, nonzero exit, early end of turn, a reply after the
  caller's deadline, no reply;
- tool calls in unusual orders: waiting on an unknown handle, parallel polls,
  edits after submit, submits after a stop.

## Fault schedules

A fault schedule is declarative data: which call, which fault, when on the
injected clock. It comes from a seed (or a Hypothesis strategy, so failures
shrink). Cluster faults to cover: submit failure, transient ssh or rsync
failure, a job stuck pending, a job killed mid-run, a cancel that fails, a
path the file broker rejects. Evaluator faults: crash, hang, garbage output,
partial results only. Process faults: SIGINT, SIGTERM, or SIGHUP at any point,
and a hard kill followed by a resume.

## Invariants

Assert properties of the whole run from its records, for every seed:

- the run ends in a typed terminal status, and "completed" means work was done;
- every capability offered was either used or withdrawn after a typed
  `unsupported`;
- every agent-visible result traces to an agent output or a typed host
  outcome, never a fabricated one;
- every submitted job ended completed or cancelled;
- budgets hold, beyond any documented refund;
- saved state loads and resumes after a crash at any point;
- every file a prompt points to exists when the turn starts;
- nothing is submitted after a stop is requested.

## Determinism and cost

Seeded randomness and the injected clock only: no sleeps, no wall-clock
waits, no network, no real agents. A failure prints its seed and a one-line
repro command. A fixed set of seeds runs in PR CI within a small time budget;
larger sweeps are opt-in. A seed that once failed stays in the fixed set as a
regression test.
