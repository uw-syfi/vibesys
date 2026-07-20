# VibeSys Skill Metadata

This page documents VibeSys-specific metadata for routing bundled Agent
Skills. It is not a general Agent Skills spec and does not add fields to
`SKILL.md` frontmatter.

VibeSys metadata lives in optional sidecar files named `.vibesys.toml`.
Standard agents and skill loaders that do not understand VibeSys metadata can
ignore these sidecar files.

## Compatibility

Do not put VibeSys routing fields in `SKILL.md` frontmatter. Keeping metadata
in a sidecar avoids collisions with current or future Agent Skills metadata and
keeps vendored skills byte-for-byte compatible with upstream.

This is especially important for vendor or submodule skill packs. Place the
sidecar in a VibeSys-owned wrapper directory and point rules at the vendored
subtree:

```text
resources/skills/vendor-pack/
├── .vibesys.toml
├── update.sh
└── skills/
    ├── vendor-skill-a/
    │   └── SKILL.md
    └── vendor-skill-b/
        └── SKILL.md
```

## Rules

Each `.vibesys.toml` contains one or more path-scoped rules:

```toml
[[rule]]
path = "skills"
backends = ["trainium"]
domains = ["llm-serving"]
```

Semantics:

- `path` is required and is relative to the sidecar file's directory.
- `path` must stay inside the sidecar directory; absolute paths and `..` are
  invalid.
- `backends` is optional. If absent, the rule does not constrain backend.
- Every backend value must match a `ComputeBackend` value: `cuda`, `metal`,
  `trainium`, or `cpu`.
- `domains` is optional. If absent, the rule does not constrain domain.
- Every domain value must match a registered domain: `generic`, `llm-serving`,
  or `microservices`.
- A skill with no matching rule is globally eligible and may load for any
  backend and domain.
- When a rule declares both `backends` and `domains`, both constraints must
  match for the skill to load.
- If multiple rules match a skill, the rule with the longest resolved `path`
  wins.
- If multiple same-specificity rules define conflicting constraints, validation
  fails rather than guessing.

Example for the vendored AWS Neuron skills:

```toml
# resources/skills/neuron-agentic-development/.vibesys.toml
[[rule]]
path = "skills"
backends = ["trainium"]
```

The sidecar is outside `skills/`, so `update.sh` can delete and recreate the
vendored `skills/` subtree without deleting VibeSys routing metadata.

Example for a domain-specific top-level skill:

```toml
# resources/skills/.vibesys.toml
[[rule]]
path = "serving-systems"
domains = ["llm-serving"]
```

## Validation

VibeSys validates standard skill frontmatter and sidecar metadata before
materializing skills into an experiment workspace. Validation fails with the
offending path when:

- `SKILL.md` YAML frontmatter delimiters are missing.
- `SKILL.md` YAML frontmatter is malformed.
- `.vibesys.toml` is malformed TOML.
- `.vibesys.toml` uses unknown top-level keys.
- a rule is missing `path`, points outside its directory, or points at a
  nonexistent path.
- `backends` is present but is not a list.
- `backends` contains an unknown backend name.
- `domains` is present but is not a list.
- `domains` contains an unknown domain name.

The repository test suite validates every `SKILL.md` and `.vibesys.toml`
under `resources/skills/` so metadata drift is caught in CI.
