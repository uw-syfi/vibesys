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

In this repository the wrappers live in `libs/vs-faults` (import
`vs_faults.api`): one `FaultPlan` (a seed plus rules naming a boundary, a
target, the ordinal of the matching call, and the fault) drives
`FaultyAgentClient` (any agent client implementation), `FaultyToolDispatch` (a tool
dispatcher keyed by tool name), and `python -m vs_faults.connector PLAN STATE
-- INNER...` (any Slurm connector command). `generated_replies(plan)` answers
a Fake agent client from each turn's declared schema. The dynamic loop's
sweep (`tests/vibesys/orchestration/dynamic/loop/test_chaos.py`,
`scripts/chaos_dynamic_loop.sh N`) shows how to compose them.

## Composed-system chaos

Compose the shell, core, and interchangeable I/O implementations through their
public interfaces. Keep these tests separate from the pure core properties in
[properties-and-goldens.md](properties-and-goldens.md), following the
[functional core rule](../../software-design/references/functional-core.md).
Inject implementation failures and unknown exceptions, including failures after
external acceptance but before acknowledgement. Check that the shell feeds
typed outcomes back as events and the run reaches a typed terminal state.
Unknown outcomes cannot become success; unresolved work remains explicit and
recoverable.

Crash and restart at every durable intent boundary. Preserve external state
across restart and assert unfinished requests are reconciled or replayed
idempotently, with one logical completion. These tests verify composition;
[contract suites](fakes-and-contracts.md) verify each implementation's promises.

## Deterministic simulation

Drive a whole composed run from one seeded schedule, as FoundationDB and
TigerBeetle do, instead of one scripted fault per test.

- One seed drives every boundary wrapper in the run: crashes, delays,
  reordering of completions, and external-state lag (queue wait, teardown
  lag), all on the injected clock.
- Generate crash points from the run's own requests and durable writes, so a
  new request kind is covered with no new test code.
- After the last fault, heal: the run must reach a typed terminal state in
  bounded virtual time, and its terminal summary must match the crash-free
  run's. Also crash inside recovery and restart again.
- Check "no orphan waits" after every step (see
  [functional-core.md](../../software-design/references/functional-core.md)).
- CI runs N seeds. A failing seed prints and replays exactly.
- Build on `vs-faults` (`FaultPlan`); do not add a second mechanism.

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

## Tool servers without agents

Test an agent tool server by synthesizing tool calls, not by running an agent:

- Generate each call's arguments from the tool's input schema with
  Hypothesis, including values a well-behaved agent would never send:
  unknown ids, other roles' handles, extreme sizes, duplicates.
- Generate sequences of calls across tools, including concurrent and
  out-of-order sequences, such as await before submit, cancel twice, or
  submit after stop. A stateful Hypothesis machine fits this well.
- Assert properties on every reply:
  - it is a typed outcome or a typed error, never an unhandled exception;
  - it stays within the size limit;
  - it is decided within the deadline;
  - an unauthorized or unsupported call is refused, and no call changes
    state it does not own.

Drive the server through its public interface against the service's Fake,
and let the service's own invariants decide pass or fail.

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

- the run ends in a typed terminal state, and "completed" means work was done;
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
