# Control state, folds, cancellation and bounded waits

Open this when writing or changing a machine, a fold, or any asynchronous
operation in the clients. The design of record is the "State" section of
[clients-architecture.md](../../../../docs/contributing/clients-architecture.md),
which also lists XState pitfalls (parallel regions, `reenter`, guards under
`exactOptionalPropertyTypes`); this file is the working checklist.

## Machines

Control state is an XState v5 machine written as
`setup({types, actors, actions, guards, delays}).createMachine(...)`.

- Every operation is an invoked actor with an explicit `id`. Declare it with
  `unprovided` from `@vibesys/machine` and supply the real one
  from the shell with `provide`, so the machine stays pure and the operation
  injectable. Type the placeholder with the operation's input and output, or
  `provide` rejects the real one.
- A delay is an `after` transition. The clock comes in through `createActor`'s
  `clock` option, adapted from an injected `ScheduleTimeout` by `clockFrom`.
- A view reads the actor through `actorStore`, which notifies only when the
  view's selection changes.
- Every invoke has both `onDone` and `onError`. The shell reports expected
  failures as output (`{ok: false, error}`); `onError` handles an unexpected
  rejection the same way, so a throw ends the flight instead of leaving its
  region waiting.

Examples to read: the session machine in `@vibesys/run-session` (start-up,
recovery and snapshot fencing as parallel regions), the host-connection
machine in `@vibesys/connection`, and the run-control feature's machine
(latest command wins).

## Folds

A data fold is a pure `(state, action) -> state` reducer on `@vibesys/store`
(`createStore`), in its own module, tested with `checkModel` from
`@vibesys/store/testing`. Wrapping a fold in a one-state machine adds a
runtime and checks nothing more. `@vibesys/run-session` keeps its fold in a
module apart from its machine.

Features not yet migrated still run control state as reducers with request
tokens. The clients Biome configuration shows which features the control-state
rules already cover. Migrate one
feature per change, keeping its seeded invariants (ported to `checkMachine`)
and deleting the reducer in the same change.

## Cancellation

Cancellation is ownership: whoever starts work owns an abort signal for it, and
ending the owner aborts the work.

- In a machine, leaving the state that owns an operation stops it and cancels
  its delays; a stopped operation never reports, so no reply token is needed.
- XState drops a late output but cannot stop the promise that produces it. An
  operation that opens a resource returns it, and the machine installs it in
  `onDone`, from where leaving the state disposes it. An operation reads its
  `signal` and disposes what it opened when that aborts.
- An outcome can go stale between the operation ending and the machine taking
  it. Guard `onDone` with a guard the shell provides and add a fallback
  transition whose action hands the resource back: the `current` guard and
  `discard` action in the host-connection machine of `@vibesys/connection`.
- Outside machines, pass the signal down every layer: a dial takes the signal
  its relay owns (the run-control feature's relay), and a snapshot load takes
  the signal of the state that asked for it (the `RunConnection` port of
  `@vibesys/run-session`). Attach abort listeners with `registerAbort` from
  `@vibesys/transport`, which has no gap between checking `aborted` and
  listening.
- Shutting down a stream composition closes both the stream and its transport
  even if one close fails: use `closeStreamAndTransport` from
  `@vibesys/transport`.
## Bounded waits

Every wait ends: it completes, fails, or hits a deadline. That covers
bootstraps and capability exchanges, dials and handshakes, writes waiting on
backpressure or credit, buffers waiting for a line terminator, and process or
connection retirement.

- Inside a machine the deadline is an `after` transition.
- In shell code it is scheduled through an injected `ScheduleTimeout`, with the
  real timer only as the production default, so a test drives it with
  `FakeClock` from `@vibesys/transport/testing`. Cancel the
  deadline on every exit path, including a deadline that fires synchronously
  (the relay's tests include one that schedules a deadline that does).
- A buffer that waits for more input has a size bound as well as a time bound.
