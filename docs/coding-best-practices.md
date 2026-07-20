# Coding Best Practices

This repository values explicit contracts, reproducible validation, and small
changes that respect the existing module boundaries. Good code should make agent
behavior and evaluation outcomes easier to reason about, not just pass the happy
path.

## Architecture Boundaries

- Put framework behavior under `src/vibesys/`.
- Put reusable standalone libraries under `libs/`.
- Put prompt, loop, and domain behavior in the package that owns that surface.
- Put long-form serving knowledge under `resources/skills/`, not in framework
  code or prompt skeletons.
- Keep example targets in the standard bundle shape: `OBJECTIVE.md`,
  `vibesys.input.toml`, optional `reference/`, evaluator source directories,
  and `README.md`. Evaluator commands are declared by the manifest and may be
  problem-specific.
- Put nontrivial candidate-facing APIs, ABIs, ownership rules, and service
  protocols in `CANDIDATE_CONTRACT.md`; keep evaluator internals and trust-model
  discussion in a separate design document.
- Keep compatibility wrappers thin. New behavior should live in the canonical
  implementation module or reusable library.

## Unidirectional Data Flow

Treat unidirectional data flow as the default architecture principle. Inputs
move through core behavior into typed outputs or events, which consumers then
interpret. Dependencies should not point back from core behavior into the
adapters or presentation layers that consume its results.

- Write core orchestration against stable protocols and data contracts, not
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
  implementation names outside the composition layer that selects them.
- Introduce an abstraction when it creates a genuine ownership boundary,
  clarifies the direction of data flow, or removes meaningful duplication. Do
  not add indirection without a concrete contract to protect.

## Python Code

- Use typed Python 3.11+ patterns already present in the repo.
- Use Pydantic models for external contracts: config files, metadata files,
  structured agent responses, persisted state, and other boundary objects.
- Use `StrEnum`, `Literal`, and typed registries for closed sets instead of raw
  strings spread across call sites.
- Prefer dataclasses for small immutable internal values when validation is not
  the main concern.
- Accept `Path`-like inputs at boundaries when useful, then normalize once.
- Keep functions small enough that ownership is obvious. Add shared abstractions
  only when they remove real duplication or clarify a cross-module contract.

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
  contract or improve testability.

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

## Tests And Checks

Run the narrowest relevant test first, then broaden when the change crosses
module boundaries.

```bash
./scripts/format.sh
./scripts/check_format.sh
./scripts/check_lint.sh
uv run pytest
uv run pytest path/to/test.py
uv run pytest -k keyword
```

For prompt changes, include the snapshot diff in your review. For config,
metadata, feature flags, and persisted-state changes, test both valid input and
failure cases.

- Use shared contract tests for interchangeable implementations, especially
  sandbox strategies and compute backends.
- Test external CLI adapters with fake runners and representative success,
  missing-tool, timeout, nonzero-exit, and malformed-output cases.
- Test event changes through serialization and each affected consumer, not only
  at the producer.
- Prefer assertions about observable contracts over assertions tied to private
  implementation details.

## Avoid

- Large unrelated refactors.
- Raw strings where repo enums, registries, or typed models already exist.
- Ad hoc parsing when TOML, YAML, Pydantic, or standard library parsers are
  available.
- Silent acceptance of misspelled config or metadata.
- Mixing domain knowledge, execution policy, and interface mechanics in one
  place.
- Concrete sandbox or renderer checks in otherwise strategy-agnostic core code.
- Presentation formatting in backend event producers.
- Scattered subprocess command construction and output parsing for the same
  external tool.
- Updating generated-looking artifacts without checking what behavior changed.
