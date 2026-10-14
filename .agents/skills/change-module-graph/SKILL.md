---
name: change-module-graph
description: Add, split, move or reconnect modules in this repository's module graph (Python modules and libraries in tach.toml, TypeScript packages under clients/), and add I/O interfaces and their implementations. Use when creating or splitting a package under src/, libs/ or clients/, adding a cross-module import or tach edge, declaring a module's public interface, adding an interface or implementation to an I/O library, placing an agent tool server, or when a size limit, tach, or the package-graph check fails.
metadata:
  kind: codebase
---

# Change the module graph

The `software-design` skill says why modules are deep, declared and one-way.
This skill says how that is declared and checked here. Open
[references/python.md](references/python.md) for tach, the I/O libraries, tool
servers, Python idioms and golden examples, and
[references/typescript.md](references/typescript.md) for the client package
graph.

## Builds on

- `software-design`: rules 2 (declared interfaces), 3 (substitutability),
  4 (one-way dependencies), 5 (factor when callers need internals) and
  14 (functional core, interfaces and implementations).
- `enforce-rules`: when a graph rule should become a check.

## When this applies

- A new, split or moved Python module under `src/` or library under `libs/`,
  or a new TypeScript package under `clients/`.
- A new import that crosses modules or packages.
- A new I/O interface, or a new implementation of one.
- Placing an agent tool server.
- `scripts/check_tach.py`, the package-graph check, or a size limit fails.

## Steps

1. **Place code by ownership.** Put it in the module or library that owns the
   responsibility, or a new one, never where similar code happens to sit; the
   layout and placement rule are in
   [architecture.md](../../../docs/contributing/architecture.md).
   Unenforced: ownership needs judgment.
2. **Declare every Python module and its interface in `tach.toml`.** A module
   needs a `[[modules]]` entry; a library also needs its `source_roots` entry
   and an `[[interfaces]]` entry exposing only `api`; a new or split module
   under `src/` adds an `[[interfaces]]` entry too. `scripts/check_tach.py`
   fails on an undeclared edge or a cycle.
3. **Add the edge in the same PR as the import**, never upward and never
   closing a cycle; prefer removing an edge. Then regenerate the graph in
   architecture.md with `scripts/check_tach_graph.py --write`
   (`scripts/check_tach_graph.py --check` fails on drift).
   `tests/architecture/test_library_graph_acyclic.py` also rejects cycles
   between whole libraries that tach's module edges miss.
4. **Import a library only through `<pkg>.api`, and core only through
   `vibesys.api`.** `tach.toml` interfaces enforce the first;
   `tests/architecture/test_python_package_boundaries.py` checks package
   boundaries; `headless` and `server` never import each other (`tach.toml`).
5. **Keep the core pure.** `vs_core` imports no I/O library; its I/O goes
   through interfaces the shell calls. `scripts/check_purity.py` holds the
   core to it with a shrink-only allowlist.
6. **Name interfaces by role and implementations `<Variant><Role>`**, put the
   interface in its owning library's `.api`, and register every
   implementation in that library's contract suite. Unenforced: no check maps
   interfaces to contract suites or names to roles yet.
7. **Use the repository's vocabulary.** "protocol" means a wire protocol
   (`vs-evaluator-protocol`), "provider" an agent CLI vendor, "driver" an
   agent driver in `vs-agent`, and "agent backend" the `AgentBackend` choice
   (`cli` or `stub`); do not reuse them for interfaces. Rename the outliers
   `AgentClientProtocol` and `ComputeBackendImpl` when you touch them.
   Unenforced: term choice needs judgment.
8. **Declare a TypeScript package's layer and exports in its `package.json`.**
   `clients/tools/scripts/check_package_graph.mjs` owns the rules between
   packages and `clients/.dependency-cruiser.mjs` rejects relative paths into
   another package; run `pnpm check:ts-architecture` first, since each gate
   names what a new package is missing.
9. **Treat a size limit as a cue to factor, not a target to dodge.**
   `scripts/check_file_length.py` (with its allowlist in `pyproject.toml`),
   Ruff `C901` and `PLR0912`, and Biome's `noExcessiveLinesPerFile` hold the
   numbers.

## Testing

- Run `scripts/check_tach.py`, `scripts/check_tach_graph.py` (with
  `--check`) and `scripts/check_lint.sh` after any Python graph change.
- Run `pnpm check:ts-architecture` and `pnpm check:knip` from `clients/`
  after any TypeScript package change (`.repoctl/checks.toml`, `tui_quality`).
- Run the new interface's contract suite against every implementation,
  Fake and production, as the `write-vibesys-tests` skill describes.
  Unenforced: real implementations run only when opted in.

## Known failure modes

- A new library escapes the public-surface test:
  `tests/architecture/test_library_api_surface.py` lists library `api`
  modules by hand, so add the new one there.
- An `src/` module without an `[[interfaces]]` entry exposes all of its
  internals to any module with the edge, and later code grows to depend on
  them (`tach.toml`). Unenforced: existing modules are not yet declared, so a
  check would fail on all of them.
- A cycle through three libraries passes tach because no single module edge
  closes it; `tests/architecture/test_library_graph_acyclic.py` exists for
  that reason.
- Code that rebuilds `.vibesys` paths instead of opening them through
  `vs_project.api.Project` (`tests/architecture/test_project_state_encapsulation.py`).

## Sources of truth

- `tach.toml`: Python modules, edges and interfaces.
- [architecture.md](../../../docs/contributing/architecture.md): the layout,
  the placement rule and the generated module graph.
- [clients-architecture.md](../../../docs/contributing/clients-architecture.md):
  the client package graph and state rules.
- `clients/tools/scripts/check_package_graph.mjs`: the TypeScript package
  rules.
