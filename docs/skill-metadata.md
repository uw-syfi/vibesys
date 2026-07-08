# VibeServe Skill Metadata

This page documents VibeServe-specific metadata for routing bundled Agent
Skills. It is not a general Agent Skills spec and does not add fields to
`SKILL.md` frontmatter.

VibeServe metadata lives in optional sidecar files named `.vibeserve.toml`.
Standard agents and skill loaders that do not understand VibeServe metadata can
ignore these sidecar files.

## Compatibility

Do not put VibeServe routing fields in `SKILL.md` frontmatter. Keeping metadata
in a sidecar avoids collisions with current or future Agent Skills metadata and
keeps vendored skills byte-for-byte compatible with upstream.

This is especially important for vendor or submodule skill packs. Place the
sidecar in a VibeServe-owned wrapper directory and point rules at the vendored
subtree:

```text
resources/skills/vendor-pack/
├── .vibeserve.toml
├── update.sh
└── skills/
    ├── vendor-skill-a/
    │   └── SKILL.md
    └── vendor-skill-b/
        └── SKILL.md
```

## Rules

Each `.vibeserve.toml` contains one or more path-scoped rules:

```toml
[[rule]]
path = "skills"
backends = ["trainium"]
```

Semantics:

- `path` is required and is relative to the sidecar file's directory.
- `path` must stay inside the sidecar directory; absolute paths and `..` are
  invalid.
- `backends` is optional. If absent, the rule does not constrain backend.
- Every backend value must match a `ComputeBackend` value: `cuda`, `metal`,
  `trainium`, or `cpu`.
- A skill with no matching rule is backend-agnostic and may load for any
  backend.
- If multiple rules match a skill, the rule with the longest resolved `path`
  wins.
- If multiple same-specificity rules define conflicting `backends`, validation
  fails rather than guessing.

Example for the vendored AWS Neuron skills:

```toml
# resources/skills/neuron-agentic-development/.vibeserve.toml
[[rule]]
path = "skills"
backends = ["trainium"]
```

The sidecar is outside `skills/`, so `update.sh` can delete and recreate the
vendored `skills/` subtree without deleting VibeServe routing metadata.

## Validation

VibeServe validates standard skill frontmatter and sidecar metadata before
materializing skills into an experiment workspace. Validation fails with the
offending path when:

- `SKILL.md` YAML frontmatter delimiters are missing.
- `SKILL.md` YAML frontmatter is malformed.
- `.vibeserve.toml` is malformed TOML.
- `.vibeserve.toml` uses unknown top-level keys.
- a rule is missing `path`, points outside its directory, or points at a
  nonexistent path.
- `backends` is present but is not a list.
- `backends` contains an unknown backend name.

The repository test suite validates every `SKILL.md` and `.vibeserve.toml`
under `resources/skills/` so metadata drift is caught in CI.
