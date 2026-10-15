# VibeSys Skill Metadata

Bundled Agent Skills live in `resources/skills/`, ship inside the VibeSys
wheel, and are installed for optimization agents during runs. A skill can be
limited to some compute backends or domains by a `[[rule]]` in an optional
`.vibesys.toml` sidecar file next to it, so `SKILL.md` frontmatter stays
standard and vendored skills stay byte-identical to upstream:

```toml
[[rule]]
path = "skills"
backends = ["trainium"]
```

How to write a sidecar, the rule semantics and precedence, every validation
error, the `references/platforms/<backend>/` layout, vendoring with
`update.sh`, and the tests to run are in the
[`add-bundled-skill`](https://github.com/uw-syfi/vibesys/blob/main/.agents/skills/add-bundled-skill/SKILL.md)
agent skill and its
[sidecar rules reference](https://github.com/uw-syfi/vibesys/blob/main/.agents/skills/add-bundled-skill/references/sidecar-rules.md).
The code that enforces them is `src/vibesys/orchestration/skill_selection.py`.
