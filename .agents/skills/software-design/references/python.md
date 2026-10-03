# Python

Boundary tool: tach (`tach.toml`). Lint: ruff. Types: ty.

## Size cues

| Cue | Value | Enforced by |
| --- | --- | --- |
| Package size | About 10k non-test lines, a cue to ask rule 5, not a rule | Nothing. Measure with `find <pkg> -name '*.py' -not -path '*/tests/*' \| xargs wc -l` |
| File length | 2000 lines; two files are allowlisted at their recorded counts and may not grow | `scripts/check_file_length.py`, `[tool.vibesys.file_length]` |
| Function | Complexity 10 (`C901`); ruff `PLR` defaults of 12 branches, 50 statements, 5 arguments | ruff, `scripts/check_lint.sh` |

Crossing the package cue means: split into separately declared tach modules,
each with a narrow interface, or state in the PR's `Design` section why the
package stays whole.

## Declaring and enforcing a public interface

Tach is module-granular: each `[[modules]]` entry lists its allowed
`depends_on` edges, and `uv run tach check` fails on an undeclared edge or a
cycle (`forbid_circular_dependencies`).

- **Libraries.** Each `libs/*` package exposes only `<pkg>.api` through an
  `[[interfaces]]` entry (`expose = ["api", "api\\..*"]`). Import a library
  only through its `api`.
- **Modules under `src/`.** These have no `[[interfaces]]` entries today, so
  their internals are reachable by any module with the edge. A new module, or
  one split under rule 5, must add an `[[interfaces]]` entry the same way the
  libraries do. Do not treat an existing module's lack of one as license.
- **Facades.** `vibesys.api` is the only core module other packages import.
  `server`, `headless`, and `entrypoints` reach core through it.

## Adding an edge

1. Add the import.
2. Add the target to the importer's `depends_on` in `tach.toml`, in the same PR.
   Prefer removing an edge to adding one. Never add an upward edge or a cycle;
   move the shared code down.
3. `uv run tach check`
4. `uv run python scripts/check_tach_graph.py --write`, then commit the updated
   graph in `docs/contributing/architecture.md`.

A new module also needs its `[[modules]]` entry (and `[[interfaces]]`), and a
new library needs its `source_roots` entry.

## Idioms

- **Boundary types.** Pydantic models for external contracts, with
  `model_config = ConfigDict(extra="forbid")` so unknown keys are rejected.
- **Closed sets.** `StrEnum` or `Literal`, never raw strings. Look up
  implementations in a registry keyed by the enum. Match exhaustively.
- **Errors.** Typed errors that name the offending key or path
  (`vibesys.errors.ConfigurationError` carries a diagnostic).
- **Resources.** Context managers or an explicit `close()`; cleanup must be
  idempotent.
- **Effects.** Inject the subprocess runner, clock, or client through a
  constructor argument or parameter, and ship a Fake beside the interface.

## Golden examples

Named by symbol; read them before writing something similar.

| Rule | Look at |
| --- | --- |
| 1, 2 | `vibesys.api` (facade with a documented contract); `vs_agent.api` |
| 3 | `vibesys.backends.ComputeBackendImpl` (a Protocol that callers use without knowing the implementation) |
| 4 | The `tach.toml` module graph; `entrypoints` reaching core only through `vibesys.api` |
| 5 | The `libs/` packages, each a separately declared unit behind `<pkg>.api` |
| 6 | The `vibesys.backends` registry (`register`, `get`) and `vibesys.domains.registry.DOMAINS` keyed by enums |
| 7 | The `server.api.protocol` models (`extra="forbid"`) |
| 8 | `server.api.protocol` to `protocol.schema.json` to generated TypeScript types (`pnpm generate:protocol`); `vs_project.Project.open` as the only way to reach the `.vibesys` layout |
| 9 | `vs_agent.api.testing` (`FakeAgentClient`) |
| 10 | `vibesys.features` (`FeatureFlag` enum plus `FEATURES` registry) for gating a change |

## Commands

```bash
uv run tach check
uv run python scripts/check_tach_graph.py --check   # --write after editing tach.toml
uv run python scripts/check_file_length.py
./scripts/check_lint.sh
./scripts/check_types.sh
```
