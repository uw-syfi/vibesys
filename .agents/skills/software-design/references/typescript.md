# TypeScript

TypeScript idioms for the design rules, in a workspace of packages.

## Declaring and enforcing a public interface

- A package's public interface is its `exports` in `package.json`. Importing
  anything else from another package is an error, enforced by a graph checker
  (such as dependency-cruiser) that rejects deep and relative imports into
  another package.
- One package per concept. Declare each package's layer and runtime in its
  manifest, and let one checker own the rules between packages: layer
  direction, declared and exported imports, runtime reachability, and
  confinement of third-party UI or platform libraries to the packages that
  need them.
- Derive the package set from the workspace on disk, so a new package is
  checked without a hand-maintained list.

## Size cues

A size limit is a cue to ask rule 5, not a reason to split. Configure file
length, function length, cognitive complexity and parameter counts in the
linter (Biome's `complexity` and `style` rules), so the numbers live in one
place.

## Idioms

- **Closed sets.** A string-literal union. When the values cross a process
  boundary, take the union from generated contract types, never a
  hand-written copy. Branch exhaustively so a new member is a compile error.
- **Types first.** Model a lifecycle as a discriminated union, one variant per
  state, not an object of optional fields; brand identifiers
  (`string & {readonly __brand: "CommitId"}`) and construct them only in the
  parser. See [types-and-data.md](types-and-data.md).
- **Generated types.** Never edit generated files; regenerate them from the
  source of truth and review the diff.
- **State.** Frontend state changes only from backend events; keep folds pure
  and pass time as event data. See [functional-core.md](functional-core.md).
  Ask whether the state is a mode with effects or a fold:
  - Control state (a mode that starts work, waits, retries, cancels) is a
    state machine (XState v5: `setup({types, actors, actions, guards,
    delays})`), each operation an injected `invoke` with an explicit `id`,
    delays as `after`, cancellation by leaving the state. The shell provides
    the operations with `machine.provide`.
  - A data fold (events in, a value out, no mode) is a plain reducer.
  - Test a machine with pure `getNextSnapshot`/`transition` steps; test stale
    replies on a live actor.
- **I/O.** Keep transport behind an interface called by the shell, and use an
  in-memory implementation in composed tests (see the `testing` skill).
