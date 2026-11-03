---
name: write-effect-code
description: Write or change Effect code in the Effect desktop app (services as Context tags and Layers, tagged errors, Streams for subscriptions, interruption as cancellation, atoms, TestClock tests) inside the lint and folder rules that hold it. Use when adding or changing a service, transport, atom or screen data flow in the Effect desktop app, writing an Effect test, or when an Effect lint rule or folder rule fails.
metadata:
  kind: codebase
---

# Write Effect code

The Effect desktop app (`@vibesys/desktop`) is one package with a folder per
role: plain folds, Effect services, React screens, the Electron main process
and preload, and test doubles. Its control state is Effect services and atoms;
its data folds are plain reducers ("State" in
[clients-architecture.md](../../../docs/contributing/clients-architecture.md)).
The XState packages that predate it are legacy and get no new work.

## Builds on

- `software-design` (pure core, thin shell, parse at boundaries) and `testing`
  (Fakes at boundaries, no flaky tests).
- `write-vibesys-tests` for test tiers; `enforce-rules` when a fix reveals a
  bug class a check could close.

## When this applies

- A new or changed service, layer, transport, atom or stream in the Effect
  desktop app, or a screen's data flow.
- A test of that code.
- An Effect lint rule or a folder rule failing on a change.

Adding a client package or a screen of the old desktop packages is the
`build-client-feature` skill.

## Steps

1. **Put code in the folder that owns it.** A fold goes in `state`, which
   imports nothing outside itself and never `effect`; Effect services, transport
   and atoms in `core`, which may import `state`; React screens in `ui`, which
   may import `core` and `state`; Electron code in `main` and `preload`, which
   never import `ui`. Fakes go in `testing`, which only tests import. Do not add
   a package; one needs a second real consumer, generated code or a measured
   build problem. The dependency-cruiser rules `desktop-state-imports-nothing-outside-itself`,
   `desktop-state-never-imports-effect`, `desktop-core-does-not-reach-the-shells`,
   `desktop-ui-does-not-reach-main-or-preload`,
   `desktop-main-and-preload-do-not-import-ui` and
   `desktop-production-code-does-not-import-testing` fail a misplaced import, and
   `check_package_graph` (`external-dependencies`) confines `effect` to those entries.
2. **Import Effect modules as namespaces from their subpath**:
   `import * as Effect from 'effect/Effect'`. The Grit rule `effect-namespace-imports`
   rejects the `effect` barrel and named imports of a module's members.
3. **Make a service a `Context.Service` named for its role, with one `Layer` per
   implementation.** Callers `yield*` the service and never name an implementation;
   the entry files build the layers. Unenforced: whether something deserves a
   service is a design judgment.
4. **Name each operation with `Effect.fn("Service.op")`** so it has a span and a
   stack trace. Unenforced: a lint cannot tell an operation from a local helper.
5. **Type failures.** Declare `Data.TaggedError` classes, put them in the error
   channel, and handle them with `Effect.catchTag`; leave defects uncaught.
   Unenforced: which failures are expected is judgment.
6. **Model a subscription as a `Stream` and cancel by interruption.** The fiber
   or scope that consumes it ends it, with its delays and finalizers; write no
   abort controller, reply token or flag. `effect-core-no-promises` rejects an
   `AbortController` in `core`.
7. **Wrap a Promise API once, at the boundary**, with `Effect.tryPromise` or
   `Stream.fromAsyncIterable`. `effect-core-no-promises` rejects `async`, `await`
   and `Promise` in `core`.
8. **Take time, randomness and ids from services.** Wait with `Effect.sleep` or a
   `Schedule`; never `Date.now`, a timer, `Math.random` or `crypto.randomUUID`.
   `effect-no-ambient-time-random` covers `core`, `state` and `ui`.
9. **Run an Effect only in an entry file.** Return the Effect and let
   the entry call `Effect.runFork`; `effect-run-only-in-entries` allows it in
   the two designated entry files only.
10. **Let a screen read atoms.** `ui` binds atoms from `core` with the React
    bindings and starts no work of its own; the rules `ui-no-effect-hooks`,
    `ui-no-io-calls` and `ui-no-timers` apply there.
11. **Explain any suppression** with a comment that lists each alternative and
    why it is worse. Unenforced: Biome rejects an empty explanation but cannot
    judge whether the alternatives were weighed.

## Testing

- **Write a test as `it.effect`** from `@effect/vitest`. It runs on `TestClock`;
  advance time with `TestClock.adjust` and assert the state after each step.
  `effect-tests-no-real-time` rejects wall clocks, `it.live`, polling,
  timeouts and retries in a test.
- **Seed randomness with `Random.withSeed`** and cover a fold with a seeded
  property test over generated inputs. Unenforced: the input space needs judgment.
- **Fake only at a process, network, clock, id or filesystem boundary**, as a
  `Layer` in `testing`, and run everything above it for real. Every Fake passes
  the contract suite of the interface it replaces. Unenforced: which layer is a
  boundary needs judgment.
- **Run the package's tests** with its `test` script, which goes through the
  shared Vitest runner and its hang guard (`check_test_runners`).
- **Give a bug fix a regression test** that fails at the merge base.
  Unenforced: CI runs only the head; state the merge-base result in the PR.

## Known failure modes

- A helper calls `Effect.runPromise`, starting a fiber nothing owns and that no
  test can interrupt (`effect-run-only-in-entries`).
- A `Promise` or `async` function in `core` cannot be interrupted, retried by a
  `Schedule` or timed by `TestClock` (`effect-core-no-promises`).
- A fold that imports `effect` becomes a program, and its tests need a runtime
  (`desktop-state-never-imports-effect`).
- A test that sleeps or polls passes on a fast machine and fails on a loaded
  one (`effect-tests-no-real-time`).
- `Date.now` in a service makes its behavior depend on when a test runs
  (`effect-no-ambient-time-random`).
- A forked fiber outlives its owner when forked detached. Fork children so the
  parent's scope ends them. Unenforced: a lint cannot tell an intended
  background fiber from a leak.

## Sources of truth

- [clients-architecture.md](../../../docs/contributing/clients-architecture.md):
  the folders, the State section and the rationale.
- The clients Biome configuration (which Grit rule covers which folder) and the
  dependency-cruiser configuration (the `desktop-*` rules).
- The package-graph checker (`check_package_graph`) for the confinement of `effect`.
- [references/effect-4-names.md](references/effect-4-names.md): the exact Effect 4.1.2 names this package uses.
- The Effect library's own documentation for the current API of each module.
