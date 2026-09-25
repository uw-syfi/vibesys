# TypeScript

Workspace: pnpm packages under `clients/` (`backend-client`, `core-state`,
`tui`, `web`). Lint: Biome. Boundaries: dependency-cruiser and a package
manifest check.

## Size cues

| Cue | Value | Enforced by |
| --- | --- | --- |
| Package size | A package that callers must understand from the inside, or that mixes concerns, is a cue to ask rule 5 | Nothing |
| File length | 2000 lines, test files exempt | Biome `style/noExcessiveLinesPerFile` |
| Function | 80 lines (blank lines skipped), cognitive complexity 15, 6 parameters, test files exempt from the length rule | Biome `complexity/*` |

## Declaring and enforcing a public interface

A package's public interface is its `exports` in `package.json`. Importing
anything else in another package is an error.

- **Layering.** `backend-client` is the lowest layer; `core-state` sits on it;
  `tui` and `web` sit on both and do not import each other. Rules live in
  `clients/.dependency-cruiser.cjs`.
- **No deep imports.** `workspace-packages-use-public-exports` and
  `workspace-packages-have-no-deep-imports` reject imports of another
  package's internal files or build output.
- **Allowed workspace dependencies.** `clients/scripts/check_ts_package_manifests.mjs`
  lists each package's permitted workspace dependencies and forbidden
  prefixes (for example, `core-state` may not depend on `@opentui/*`).
- **New or split package.** Add it to the manifest check and to the scanned
  set in `.dependency-cruiser.cjs`, with its `exports`, so its boundary is
  enforced from the first commit. Keep `knip` clean (no unused exports).

## Idioms

- **Closed sets.** A string-literal union. When the values cross the backend
  boundary, take the union from the generated protocol types, never a
  hand-written copy. Branch exhaustively so a new member is a compile error.
- **Generated types.** Never edit `backend-client/src/generated/`. Regenerate
  with `pnpm generate:protocol` (from `clients/backend-client`) and review the
  diff.
- **State.** Frontend state changes only from backend events; keep folds pure
  and inject clocks.
- **Effects.** Keep the transport behind an interface, and use an in-memory
  implementation in tests (see the `testing` skill).

## Golden examples

Named by module.

| Rule | Look at |
| --- | --- |
| 1, 2 | `@vibesys/core-state` (small `exports`, logic behind it) |
| 4 | The layering rules in `.dependency-cruiser.cjs` |
| 6 | The backend-neutral versus `backend-client/src/node` split behind the `./node` export |
| 8 | `@vibesys/backend-client` generated protocol types |

## Commands

From `clients/`:

```bash
pnpm check:ts                  # biome
pnpm check:ts-architecture     # dependency-cruiser + manifest check
pnpm check:knip
pnpm generate:protocol         # in backend-client, after changing protocol models
```
