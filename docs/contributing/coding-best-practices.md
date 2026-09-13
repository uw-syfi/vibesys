# Coding Best Practices

This repository values explicit contracts, reproducible validation, and small
changes that respect the existing module boundaries. Good code should make agent
behavior and evaluation outcomes easier to reason about, not just pass the happy
path.

## Architecture Boundaries

- Put headless optimization behavior under `src/vibesys/`.
- Put frontend-serving behavior under `src/server/`.
- Put process composition under `src/entrypoints/`, not in `libs/`.
- Put reusable standalone libraries under `libs/`.
- Put prompt, loop, and domain behavior in the package that owns that surface.
- Put long-form serving knowledge under `resources/skills/`, not in framework
  code or prompt skeletons.
- Keep reusable targets in their candidate repository under
  `.vibesys/tasks/<name>/`: `OBJECTIVE.md`, `vibesys.input.toml`, optional
  `reference/`, task-owned checker/benchmark programs, an optional `Dockerfile`,
  and an optional `README.md`. A task Dockerfile installs the execution
  environment only. Its build context is the task directory, while VibeSys
  mounts the current candidate repository at runtime.
  Evaluator commands run from the repository root. Reusable evaluator
  implementations belong in versioned packages, not copied task directories.
- Access the `.vibesys` layout and generated state through `vs-project`. Open one
  `Project` per repository root and use its state interface rather than
  reconstructing paths or creating independent project objects in CLI or loop
  code.
- Put nontrivial candidate-facing APIs, ABIs, ownership rules, and service
  protocols in `CANDIDATE_CONTRACT.md`; keep evaluator internals and trust-model
  discussion in a separate design document.
- Keep compatibility wrappers thin. New behavior should live in the canonical
  implementation module or reusable library.

When one part of the application describes behavior and another part applies
it, separate these roles when they have different owners or change for different
reasons:

- The **shared interface** defines the types and extension points. It contains no
  application-specific defaults or side effects.
- The **application configuration** contains the actual defaults,
  registrations, and selected values. It depends only on the shared interface.
- The **implementation** validates and applies values passed through the shared
  interface. It does not read application configuration directly.
- The **wiring code** selects the application configuration and passes it to the
  implementation.

A new configuration case should normally change the application configuration,
not add a concrete-type branch to each implementation. Do not create separate
modules merely to name these roles when the behavior is trivial and has one
owner.

## Unidirectional Data Flow

Treat unidirectional data flow as the default architecture principle. Inputs
move through core behavior into typed outputs or events, which consumers then
interpret. Dependencies should not point back from core behavior into the
adapters or presentation layers that consume its results.

- Write core orchestration against stable interfaces and typed data, not
  concrete sandbox, compute backend, renderer, or CLI implementations.
- Keep most behavior sandbox-strategy-agnostic. Sandbox-specific decisions
  belong in the sandbox implementations, their factories, or narrowly scoped
  adapters at the boundary.
- Treat backend event schemas as the frontend contract. Frontends own rendering
  and UI state and should consume only published event data; the backend should
  emit semantic information without knowing how any frontend formats, styles,
  or displays it.
- Keep cross-boundary values typed and explicit. Do not reach through an
  interface to depend on implementation details or branch on concrete
  implementation names outside the wiring code that selects them.
- Introduce an abstraction when it creates a genuine ownership boundary,
  clarifies the direction of data flow, or removes meaningful duplication. Do
  not add indirection without a shared interface to protect.
- Keep one source of truth per fact. Store the minimal state and derive the
  rest with a named selector or predicate. Do not add a field whose value is a
  function of existing fields, such as an `isEmpty` flag beside a list, a
  count beside the collection it counts, or a boolean that restates which
  member of an enum is active. Every extra writer is a place the copies can
  drift. Cache a derived value only when profiling shows the derivation is
  too costly, and then document what invalidates the cache.
- Represent closed sets as enums in both languages: `StrEnum` or `Literal` in
  Python, and a string-literal union in TypeScript. When the values cross the
  backend boundary, the union comes from the generated protocol types, not a
  hand-written copy. Predicates over a closed set should branch exhaustively
  so that a new member is a compile error rather than a silent default.

## Python Code

- Use typed Python 3.12+ patterns already present in the repo.
- Use Pydantic models for external contracts: config files, metadata files,
  structured agent responses, persisted state, and other boundary objects.
- Use `StrEnum`, `Literal`, and typed registries for closed sets instead of raw
  strings spread across call sites.
- Prefer dataclasses for small immutable internal values when validation is not
  the main concern.
- Accept `Path`-like inputs at boundaries when useful, then normalize once.
- Keep functions small enough that ownership is obvious. Add shared abstractions
  only when they remove real duplication or clarify a shared interface.

## Validation And Failure Modes

- Reject unknown config, metadata, and routing keys instead of silently ignoring
  them.
- Validate early, with errors that name the offending path, key, flag, backend,
  or contract field.
- Preserve typed feature-flag usage: add enum members and `FeatureDefinition`
  entries together, and use `FeatureFlag.X` at call sites.
- Avoid implicit fallback behavior for agent-visible contracts unless the
  fallback is documented and tested.

## Contract Ownership And Evolution

- Keep one authoritative definition for cross-language and cross-process
  contracts. Generate downstream types from it where practical instead of
  maintaining parallel handwritten schemas.
- Keep event and protocol payloads semantic rather than presentation-ready.
  Formatting, truncation, colors, labels, and layout belong to consumers.
- Prefer backward-compatible, additive contract changes. When a change is
  incompatible, update the protocol version and make the compatibility boundary
  explicit.
- Test serialized contracts at the boundary: round-trip representative payloads
  and exercise the consumers that depend on them.

## External CLI Tools

Subprocess calls are an integration boundary. When an external CLI interaction
is repeated, stateful, parsed, or otherwise significant, put it behind a focused
Python interface instead of spreading command construction and output parsing
through business logic.

- Give the wrapper typed inputs and domain-level return values. Translate
  missing executables, timeouts, nonzero exit codes, and malformed output into
  actionable domain errors.
- Document the public interface clearly, including prerequisites, side effects,
  return values, failure modes, and whether operations are idempotent.
- Build commands as argument sequences rather than interpolated shell strings
  unless shell behavior is required. Set the working directory, environment,
  text encoding, and timeout deliberately.
- Preserve useful command diagnostics, but never expose credentials, tokens, or
  other sensitive environment values in logs or exceptions.
- Make the command runner injectable when useful so tests can cover behavior
  without requiring the real external tool.
- Keep trivial, one-off process calls local when a wrapper would not clarify a
  shared interface or improve testability.

## Resource Lifecycle

- The component that creates a sandbox, subprocess, thread, temporary resource,
  or event subscription owns its cleanup.
- Cleanup must be deterministic and cover success, failure, timeout, and
  cancellation paths. Prefer context managers or explicit `close()` protocols.
- Make cleanup idempotent when callers may retry or unwind partially completed
  setup. Do not leave ownership or process lifetime implicit.

## Prompts, Templates, And Skills

- Treat prompt templates as product behavior. Small wording changes can alter
  agent behavior.
- Keep neutral prompt skeletons separate from domain-specific context.
- When rendered prompt output intentionally changes, update the relevant prompt
  snapshots and review the fixture diff as the user-visible agent diff.
- Do not blindly accept regenerated snapshots.
- Keep `SKILL.md` router files concise. Put technical depth in linked reference
  files and keep those files scoped to one topic.
- Do not add VibeSys routing metadata to skill frontmatter; use
  `.vibesys.toml` sidecars.

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

## Tests And Checks

Run the narrowest relevant test first, then broaden when the change crosses
module boundaries.

```bash
./scripts/format.sh
./scripts/check_format.sh
./scripts/check_lint.sh
uv run python scripts/check_doc_links.py
uv run pytest
uv run pytest path/to/test.py
uv run pytest -k keyword
```

For prompt changes, include the snapshot diff in your review. For config,
metadata, feature flags, and persisted-state changes, test both valid input and
failure cases.

- Use shared contract tests for interchangeable implementations, especially
  sandbox strategies and compute backends.
- Test application configuration and implementation separately, then add a
  focused test for the wiring between them.
- Test external CLI adapters with fake runners and representative success,
  missing-tool, timeout, nonzero-exit, and malformed-output cases.
- Test event changes through serialization and each affected consumer, not only
  at the producer.
- Prefer assertions about observable contracts over assertions tied to private
  implementation details.
- A bug-fix PR must include a regression test that fails at the merge base
  and passes at the head, written at the lowest layer that reproduces the
  reported symptom. A test that only restates the fix's constants, or that
  passes unchanged on the pre-fix code, is not evidence. For the TUI, a
  symptom stated in terminal geometry (columns, rows, alignment, clipping,
  overlap) must be reproduced through the OpenTUI test renderer
  (`createTestRenderer` from `@opentui/core/testing`); a pure-function test
  over a formatter is not accepted for such symptoms.

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
| File length | `scripts/check_file_length.py`, 2,000 lines | biome `style/noExcessiveLinesPerFile`, 2,000 lines |

Test files are exempt from the two length rules in both languages: long test
modules are normal. They are still held to the complexity and parameter rules.
The thresholds live in `pyproject.toml` (`[tool.ruff.lint]`,
`[tool.vibesys.file_length]`) and `biome.json`.

### Ratchets

The escape hatches below are temporary migration debt and are being removed as
the affected code is refactored. Do not add new waivers, suppression comments,
or file-length allowlist entries for these rules. Fix new violations by
splitting or simplifying the affected code.

- **Python functions.** A site-level `# noqa: <rule>  # tracked: #288` waives a
  rule at one call site. Ruff's `RUF100` fails on a waiver that no longer
  suppresses anything, so refactoring a function requires deleting its waiver
  and the count can only shrink.
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
pnpm lint:ts
```

## Avoid

- Large unrelated refactors.
- Raw strings where repo enums, registries, or typed models already exist.
- Stored copies of state that another field already determines.
- Ad hoc parsing when TOML, YAML, Pydantic, or standard library parsers are
  available.
- Silent acceptance of misspelled config or metadata.
- Mixing a shared interface, application configuration, and side-effecting
  implementation when they have different owners or reasons to change.
- Concrete sandbox or renderer checks in otherwise strategy-agnostic core code.
- Presentation formatting in backend event producers.
- Scattered subprocess command construction and output parsing for the same
  external tool.
- Updating generated-looking artifacts without checking what behavior changed.
