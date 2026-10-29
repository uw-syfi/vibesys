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

The skills of a run's chat agent (how to read an idea, results against the base, what each
role does, how steers work) live in `resources/chat-skills/`, a separate bundled tree. Skill
discovery has no way to scope a skill to one kind of agent, so a chat skill under
`resources/skills/` would be installed for every optimization agent. The chat agent's launch
asks for them with `AuxiliaryAgentLaunch.skills`, and `vibesys.api.wiring.chat_skill_dirs`
resolves the tree; no sidecar applies, since they hold on every backend and domain.
