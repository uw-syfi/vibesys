# Coding Best Practices

This repository values explicit contracts, reproducible validation, and small
changes that respect the existing module boundaries. Good code should make agent
behavior and evaluation outcomes easier to reason about, not just pass the happy
path.

## Architecture Boundaries

- Put framework behavior under `src/vibe_serve/`.
- Put reusable standalone libraries under `libs/`.
- Put prompt, loop, and domain behavior in the package that owns that surface.
- Put long-form serving knowledge under `resources/skills/`, not in framework
  code or prompt skeletons.
- Keep example targets in the standard bundle shape: `OBJECTIVE.md`,
  `reference/`, `accuracy_checker/`, `benchmark/`, and `README.md`.
- Keep compatibility wrappers thin. New behavior should live in the canonical
  implementation module or reusable library.

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

## Prompts, Templates, And Skills

- Treat prompt templates as product behavior. Small wording changes can alter
  agent behavior.
- Keep neutral prompt skeletons separate from domain-specific context.
- When rendered prompt output intentionally changes, update the relevant prompt
  snapshots and review the fixture diff as the user-visible agent diff.
- Do not blindly accept regenerated snapshots.
- Keep `SKILL.md` router files concise. Put technical depth in linked reference
  files and keep those files scoped to one topic.
- Do not add VibeServe routing metadata to skill frontmatter; use
  `.vibeserve.toml` sidecars.

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

## Avoid

- Large unrelated refactors.
- Raw strings where repo enums, registries, or typed models already exist.
- Ad hoc parsing when TOML, YAML, Pydantic, or standard library parsers are
  available.
- Silent acceptance of misspelled config or metadata.
- Mixing domain knowledge, execution policy, and interface mechanics in one
  place.
- Updating generated-looking artifacts without checking what behavior changed.
