# Python module graph

The Python packages under `src/` and `libs/`, as `tach.toml` declares them.
Boundary tool: tach. Lint: Ruff. Types: ty.

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
`depends_on` edges, and `uv run python scripts/check_tach.py` fails on an undeclared edge or a
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
3. `uv run python scripts/check_tach.py`
4. `uv run python scripts/check_tach_graph.py --write`, then commit the updated
   graph in `docs/contributing/architecture.md`.

A new module also needs its `[[modules]]` entry (and `[[interfaces]]`), and a
new library needs its `source_roots` entry.

## I/O libraries and their interfaces

Each top-level I/O library owns its interfaces in its `.api` and its
implementations internally or in smaller libraries it fans out to:
`vs-project` owns filesystem and state persistence (open the `.vibesys` layout
only through `vs_project.api.Project`), `vs-slurm` cluster jobs, `vs-agent`
agent sessions, `vs-sandbox` containers, `vs-evaluation` evaluation execution,
`vs-github` GitHub operations, and `vs-sim` time, processes and signals. The
current list is the `libs/` tree and its `[[modules]]` entries in `tach.toml`.

Declare tach edges in the same change: shell -> core and shell -> each owning
library's interface. The core (`vs_core.api`) imports no I/O library.

## Agent tool servers

A tool server lives in its own module, in its own library under `libs/` when
other packages reuse it, or as a standalone server under `resources/` (the
profilers in `resources/profilers/`). Servers in `src/` and `libs/` build
their tools on `vs_mcp.api` (`ToolSpec`, `serve_stdio`), not a hand-built
`FastMCP`. A standalone server under `resources/` may use FastMCP directly and
shares its conventions through its family's `_common` package.

## Idioms here

The language idioms are in the `software-design` skill's Python reference.
What this repository adds:

- `tests/quality/test_models_reject_unknown_keys.py` holds every product
  model, and every dataclass or TypedDict Pydantic decodes, to
  `extra="forbid"`; a model that must accept unknown keys sets `extra`
  explicitly and declares `unknown_keys_reason: ClassVar[str]`.
- Configuration errors carry a diagnostic naming the key
  (`vibesys.errors.ConfigurationError`).

## Golden examples

Named by symbol; read them before writing something similar.

| Rule | Look at |
| --- | --- |
| 1, 2 | `vibesys.api` (facade with a documented contract); `vs_agent.api` |
| 3 | `vs_agent.api.AgentSessions` (a role-named interface with real and Fake implementations) |
| 4 | The `tach.toml` module graph; `entrypoints` reaching core only through `vibesys.api` |
| 5 | The `libs/` packages, each a separately declared unit behind `<pkg>.api` |
| 6 | `vibesys.plugin_catalog.OrchestrationRegistry` (stable ids to registrations) and `vibesys.domains.registry.DOMAINS` keyed by enums |
| 7 | The `server.api.protocol` models (`extra="forbid"`) |
| 8 | `server.api.protocol` to `protocol.schema.json` to generated TypeScript types (`pnpm generate:protocol`); `vs_project.api.Project.open` as the only way to reach the `.vibesys` layout |
| 9 | `vs_agent.api.testing` (`FakeAgentClient`) |
| 10 | `vs_sim.api.testing.hostile_json_frames` (the shared hostile-input corpus every untrusted JSON decoder runs against) |
| 14 | `vs_core.api.step` (`state + event -> new state + requests`) and the property tests in `libs/vs-core/tests/` |

## Commands

```bash
uv run python scripts/check_tach.py
uv run python scripts/check_tach_graph.py --check   # --write after editing tach.toml
uv run python scripts/check_file_length.py
./scripts/check_lint.sh
./scripts/check_types.sh
```
