# TypeScript

Workspace: pnpm packages under `clients/`, one per concept (`contracts`,
`store`, `run-state`, `transport`, `hosts`, `runs`, `run-session`, `ipc`, `ui`,
`features/*`, `apps/*`); the graph and its rules are in
[clients-architecture.md](../../../../docs/contributing/clients-architecture.md).
Lint: Biome. Boundaries: dependency-cruiser and the package-graph checker.

## Size cues

| Cue | Value | Enforced by |
| --- | --- | --- |
| Package size | A package that callers must understand from the inside, or that mixes concerns, is a cue to ask rule 5 | Nothing |
| File length | Production warns at 1500 and fails at 2000; tests fail at 10,000 | Biome `style/noExcessiveLinesPerFile` |
| Function | 80 lines (blank lines skipped), cognitive complexity 15, 6 parameters, test files exempt from the length rule | Biome `complexity/*` |

## Declaring and enforcing a public interface

A package's public interface is its `exports` in `package.json`. Importing
anything else in another package is an error.

- **Layering and runtimes.** Each package declares its layer and its entries'
  runtimes in `package.json` (`"vibesys"`); `clients/scripts/check_package_graph.mjs`
  is the one home of the rules between packages (layer direction, declared and
  exported imports, runtime reachability, third-party confinement such as no
  `@opentui/*` outside an app).
- **No deep imports.** `imports-use-exports` in the package-graph checker rejects
  a specifier its target does not export; `workspace-packages-use-public-exports`
  in `clients/.dependency-cruiser.mjs` rejects a relative path into another
  package.
- **New or split package.** The package set comes from the workspace on disk
  (`clients/scripts/workspace_layout.mjs`), so the scanned paths follow from
  the directories you create. Declare the policy the gates ask for: the
  dependency direction in the manifest check, the audited entry points in
  `knip.config.ts`, the public entry in `tsconfig.architecture.json`, and
  `build`, `check`, and `test` scripts for `pnpm -r`. Each one names the
  package if it is missing, so run `pnpm check:ts-architecture` first.

## Idioms

- **Closed sets.** A string-literal union. When the values cross the backend
  boundary, take the union from the generated protocol types, never a
  hand-written copy. Branch exhaustively so a new member is a compile error.
- **Generated types.** Never edit `contracts/src/generated/`. Regenerate
  with `pnpm generate:protocol` (from `clients/contracts`) and review the
  diff.
- **State.** Frontend state changes only from backend events; keep folds pure
  and pass time as event data. See [functional-core.md](functional-core.md).
- **I/O.** Keep transport behind an interface called by the shell, and use an
  in-memory implementation in composed tests (see the `testing` skill).

## Golden examples

Named by module.

| Rule | Look at |
| --- | --- |
| 1, 2 | `@vibesys/run-state` (small `exports`, logic behind it) |
| 4 | The layering rules in `.dependency-cruiser.mjs` |
| 6 | The runtime-neutral `@vibesys/transport` versus the Node-only `@vibesys/transport-node` |
| 8 | `@vibesys/contracts` generated protocol types |

## Commands

From `clients/`:

```bash
pnpm check:ts                  # biome
pnpm check:ts-architecture     # dependency-cruiser + manifest check
pnpm check:knip
pnpm generate:protocol         # in contracts, after changing protocol models
```
