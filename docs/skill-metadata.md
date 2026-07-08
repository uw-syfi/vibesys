# VibeServe Skill Metadata

This page documents VibeServe-specific extensions to standard Agent Skills
frontmatter. It is not a general Agent Skills spec.

VibeServe metadata lives under the optional `vibeserve` namespace in
`SKILL.md` YAML frontmatter:

```yaml
---
name: example-skill
description: Example skill description.
vibeserve:
  backends: [trainium]
---
```

## Compatibility

The `vibeserve` block is optional and namespaced. Standard Agent Skills that
only define fields such as `name` and `description` continue to work unchanged.
Agents and skill loaders that do not understand VibeServe metadata should ignore
the `vibeserve` block.

Do not add bare top-level VibeServe routing fields such as `backends`. Keep
VibeServe-only fields under `vibeserve` to avoid collisions with current or
future Agent Skills metadata.

## `vibeserve.backends`

`vibeserve.backends` restricts a skill to one or more VibeServe compute
backends.

```yaml
vibeserve:
  backends: [trainium]
```

Semantics:

- Missing `vibeserve.backends` means the skill is backend-agnostic and may load
  for any backend.
- Present `vibeserve.backends` must be a YAML list.
- Every value must match a `ComputeBackend` value: `cuda`, `metal`, `trainium`,
  or `cpu`.
- A skill with `vibeserve.backends: [trainium]` is loaded only when the run uses
  `--backend trainium`.

## Validation

VibeServe validates skill frontmatter before materializing skills into an
experiment workspace. Validation fails with the offending `SKILL.md` path when:

- YAML frontmatter delimiters are missing.
- YAML frontmatter is malformed.
- `vibeserve` is present but is not a mapping.
- `vibeserve.backends` is present but is not a list.
- `vibeserve.backends` contains an unknown backend name.

The repository test suite also validates every `SKILL.md` under
`resources/skills/` so metadata drift is caught in CI.
