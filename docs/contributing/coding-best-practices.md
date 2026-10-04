# Coding Best Practices

Agent-facing design and testing guidance lives in skills, loaded on every code
change:

- [`software-design`](https://github.com/uw-syfi/vibesys/blob/main/.agents/skills/software-design/SKILL.md):
  module boundaries, public interfaces, data flow, and fitting a change to the
  existing design.
- [`testing`](https://github.com/uw-syfi/vibesys/blob/main/.agents/skills/testing/SKILL.md):
  public-API tests, Fakes instead of mocks, property-based tests, no flakiness.
- [`AGENTS.md`](https://github.com/uw-syfi/vibesys/blob/main/AGENTS.md): the
  short always-true rules. The Python module graph is in
  [architecture.md](architecture.md).

This page keeps what has no better home: placement, task-triggered rules,
size limits, lint waivers, and doc links.

## Where Things Go

- Process composition (CLI parsing, `RunRequest` building, mode entries) lives
  in `src/entrypoints/`, not `libs/`. Prompt, loop, and domain behavior lives
  in the package that owns that surface.
- Long-form serving knowledge lives under `resources/skills/`, not in framework
  code or prompt skeletons.
- Reusable targets live in their candidate repository under
  `.vibesys/tasks/<name>/`: `OBJECTIVE.md`, `vibesys.input.toml`, optional
  `reference/`, task-owned checker and benchmark programs, an optional
  `Dockerfile`, and an optional `README.md`. A task Dockerfile installs the
  execution environment only; its build context is the task directory, and
  VibeSys mounts the candidate repository at runtime. Evaluator commands run
  from the repository root. Reusable evaluator implementations belong in
  versioned packages, not copied task directories.
- Nontrivial candidate-facing APIs, ABIs, ownership rules, and service
  protocols go in `CANDIDATE_CONTRACT.md`; evaluator internals and trust-model
  discussion go in a separate design document.
- Library-to-library edges are declared in `tach.toml`. Reject upward
  library-to-application edges and cycles in review.
- Keep compatibility wrappers thin; new behavior lives in the canonical module.

## External CLI Tools

When a subprocess interaction is repeated, stateful, parsed, or otherwise
significant, put it behind a focused Python interface. One-off calls stay local.

- Typed inputs and domain-level return values. Translate missing executables,
  timeouts, nonzero exits, and malformed output into actionable domain errors.
- Argument sequences, not interpolated shell strings, unless shell behavior is
  required. Set the working directory, environment, encoding, and timeout
  deliberately.
- Keep useful command diagnostics, but never put credentials or tokens in logs
  or exceptions.
- Make the runner injectable and test with a Fake covering success,
  missing-tool, timeout, nonzero-exit, and malformed-output cases.

## Contracts

- Test serialized contracts at the boundary: round-trip representative payloads
  and exercise each consumer, not only the producer.

## Prompts, Templates, And Skills

- Prompt templates are product behavior; small wording changes alter agent
  behavior. Keep neutral skeletons separate from domain-specific context.
- When rendered output intentionally changes, update the prompt snapshots and
  review the fixture diff as the user-visible agent diff. Do not blindly accept
  regenerated snapshots.
- Keep `SKILL.md` router files concise, with depth in linked reference files
  scoped to one topic. Do not add VibeSys routing metadata to skill
  frontmatter; use `.vibesys.toml` sidecars.

## Documentation Links

`docs/` is rendered twice: by GitHub, and by the Docusaurus site at
docs.vibing.systems, which serves `docs/` as its entire content root. A
relative link from `docs/` to anything outside `docs/` resolves on GitHub and
404s on the site.

- Inside `docs/`, link to sibling docs relatively (`cli-flags.md`); link to
  anything else in the repository by absolute URL
  (`https://github.com/uw-syfi/vibesys/blob/main/...`).
- Elsewhere in the repository, relative links are fine.
- `scripts/check_doc_links.py` enforces both rules, including `#anchor`
  targets and whether an absolute repo URL still points at a file that exists.
  Its scope is Markdown link syntax only: it strips fenced blocks and inline
  `code` spans first, so nothing written inside backticks is checked there.

## Code Citations

Cite code by symbol, not by line: a line range keeps resolving after the file
grows and then names unrelated code.

- Write a citation as one inline code span, `<path>:<symbol>`
  (`unix_jsonl.py:_write_message`). The path may be the full repo-relative one
  or any unambiguous suffix of it, and the symbol may be qualified by its owner
  (`unix_jsonl.py:_RequestHandler.handle`).
- `scripts/check_doc_citations.py` enforces that the symbol is still defined in
  the cited file, and rejects a line-number citation outright. It gates the docs
  listed in its `GATED_DOCS`; add a doc there once its citations are swept.
  `python3 -m scripts.check_doc_citations .` surveys the whole repository, which
  reports citations into vendored trees that are not in this repository.

## Checks

Run the narrowest relevant test first, then broaden when the change crosses
module boundaries.

Before opening or updating a PR, run the one command that runs CI's Python
checks (`python_quality` and `python_types` in `.repoctl/checks.toml`, through
`support/repoctl`, which needs Go). It stops at the first failing command and
names it. It does not run the full test suite.

```bash
./scripts/check_ci.sh
```

Faster commands for iterating:

```bash
./scripts/format.sh
./scripts/check_format.sh
./scripts/check_lint.sh
./scripts/check_types.sh
uv run tach check
uv run python scripts/check_doc_links.py
python3 -m scripts.check_doc_citations
uv run pytest path/to/test.py
```

## Size And Complexity Limits

God files, god functions, deep branching, and long parameter lists are enforced
by the linters already in the toolchain, not by review alone. Both languages use
a shrink-only ratchet: existing violations are grandfathered at a recorded site,
and the waiver becomes an error once it is no longer needed.

| Metric | Python | TypeScript |
| --- | --- | --- |
| Cyclomatic / cognitive complexity | ruff `C901` (max 10) and `PLR0912` (max 12 branches) | biome `complexity/noExcessiveCognitiveComplexity`, max 15 |
| Function length | ruff `PLR0915`, max 50 statements | biome `complexity/noExcessiveLinesPerFunction`, 80 lines, blanks skipped |
| Parameters | ruff `PLR0913`, max 5 | biome `complexity/useMaxParams`, max 6 |
| File length | `scripts/check_file_length.py`, 2,000 lines | biome `style/noExcessiveLinesPerFile`, warn at 1,500 and fail at 2,000 production lines; fail at 10,000 test lines |

Python test files are exempt from the two length rules. TypeScript test files are
exempt from function length and use the higher file cap above. Tests are still
held to the complexity and parameter rules.
The thresholds live in `pyproject.toml` (`[tool.ruff.lint]`,
`[tool.vibesys.file_length]`) and `biome.json`.

### Ratchets

These ratchets keep violations from growing and reject stale suppressions. Fix
new violations directly by default. A lint suppression is an explicit opt-out
and is allowed only when reasonable lint-compliant fixes would make the design
more hacky than keeping the current code. List the alternatives considered and
explain why each would be worse in the source rationale. Effort, time, and
pre-existing violations are not sufficient reasons.

- **Python functions.** A site-level `# noqa: <rule>` waives a rule at one call
  site. Ruff's `RUF100` fails on a waiver that no longer suppresses anything,
  so refactoring a function requires deleting its waiver.
- **Python Ruff suppressions.** Every `# noqa` directive in Ruff's Python file
  set, including a file-level `# ruff: noqa`, has a unique ID and a specific
  reason beside the directive:

  ```python
  run_fixed_command()  # noqa: S603  # LW-000042; keep argv execution at this boundary.
  # > A helper adds indirection without changing this command boundary; shell=True
  # > weakens the argv safety guarantee. Keeping the direct call is simpler.
  ```

  For import suppressions, put the rationale in a nearby standalone comment so
  Ruff's import sorter can still process the import block:

  ```python
  # lint-waiver: LW-000043 [PLC0415]; keep the optional import inside its guarded path.
  # > Moving it to module scope imports the dependency unconditionally; a loader
  # > wrapper obscures this single guarded use without adding reuse.
  def load_optional_backend():
      from optional_backend import Backend  # noqa: PLC0415
  ```

  Keep the ID if the suppression moves within a file. The source rationale is
  required even when the waiver is temporary. Continue it on
  following `# > ...` comment lines. CI runs
  `uv run python scripts/check_lint_waivers.py` (the `python-checks` job) to
  check rationale presence, rule annotations, and ID uniqueness across the
  repository. Reviewers assess whether the listed alternatives justify the
  opt-out; the scanner cannot judge the rationale's substance.
- **TypeScript.** A `// biome-ignore lint/<group>/<rule>: pre-existing; tracked: #288`
  comment waives one site. Biome reports a suppression that no longer
  suppresses anything as an unused-suppression diagnostic, so stale waivers
  surface in `pnpm lint:ts` output and should be deleted with the refactor.
- **Python file length.** `[tool.vibesys.file_length.allowlist]` in
  `pyproject.toml` records each over-ceiling file at its current line count,
  with a comment saying what it holds. `scripts/check_file_length.py` fails when
  a non-allowlisted file crosses 2,000 lines, when an allowlisted file grows past
  its recorded count, and when an entry is stale (the file is gone or now fits
  under the ceiling, so the entry must be deleted). Shrinking a file is always
  allowed; the check prints the entries whose recorded count can be lowered.

```bash
uv run python scripts/check_file_length.py
(cd clients && pnpm lint:ts)
```
