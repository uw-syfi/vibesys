# Skills

Everything in this directory is published as the `vibesys` plugin and as a set of Agent Skills,
and nothing outside it is. Claude Code and Codex copy a plugin's source directory when they
install it, so a skill that lives elsewhere in the repository cannot reach a user.

## Install

Pick the tool you run. Each reads the same skills.

| Tool | Install | Skill names |
| --- | --- | --- |
| Claude Code | `claude plugin marketplace add uw-syfi/vibesys`, then `claude plugin install vibesys@vibesys` | `vibesys:optimize` |
| Codex | `codex plugin marketplace add uw-syfi/vibesys`, then `codex plugin add vibesys@vibesys` | `vibesys:optimize` |
| Any agent, through [`npx skills`](https://github.com/vercel-labs/skills) | `npx skills add uw-syfi/vibesys/skills --skill '*' --agent <agent>` | `optimize` |

Codex reads the `.claude-plugin/marketplace.json` described below, so the repository has one
manifest for both. `npx skills` copies each skill into the chosen agent's skills directory
(`.agents/skills/` for Codex; add `--global` for the user-level one), with no `vibesys:` prefix:
read `vibesys:measure` in a skill as the skill `measure`. Give it the `/skills` path, as above,
because the repository also holds developer skills in `.agents/skills/` that
`npx skills add uw-syfi/vibesys` would offer too. `--list` shows what a command would install.

The optimize loop needs a harness with subagents and background commands, git and Python 3.10 or
newer for its standard-library measurement scripts. It does not need the VibeSys Python package.

| Harness | Domain skills | `optimize` loop |
| --- | --- | --- |
| Claude Code | supported | supported |
| Codex | supported | harness mechanisms written from Codex's docs and source; **untested end to end** |
| Other agents (`npx skills`) | supported | needs a harness file for that agent, as described below |

## Harness neutrality

The search skills are harness-neutral. A harness is the program running the agent loop (Claude
Code, Codex, ...), not the model. The skills describe abstract capabilities; a skill that
needs any carries `references/harness/<harness>.md`, and its `SKILL.md` points there in one
sentence. Skills with no harness need have no such folder. The plugin packaging
(`.claude-plugin/`) is Claude Code's delivery format, which Codex and `npx skills` also read, and
is the only Claude-specific part outside those folders.

A harness file answers only the capabilities its skill uses, under these headings:

1. Dispatch a subagent with a fresh context
2. Run a long command as a tracked background task
3. Locate a skill's own scripts
4. Schedule periodic work
5. Wait for work without polling
6. Packaging

To add a harness, copy an existing file of the skill to `<harness>.md`, named after the harness,
keep the headings and wording, and fill each with that harness's mechanism. When a capability is
missing, say what to do instead (no scheduler: report whenever a task finishes). Provider and
tool names appear only in these files. If a model ever needs different handling, add it as a
short section in the harness file.

## Layout

- `domains/`: domain skills for kernels, LLM serving, microservices, Tokio services and
  Neuron. Both the plugin and VibeSys runs use them: a run reads only this tree, and the
  sidecar in it decides which skills a run installs for its backend and domain
  ([skill metadata](../docs/contributing/skill-metadata.md)).
- `search/`: the optimization loop for the plugin alone. VibeSys runs never install it,
  because they read only `domains/`. Invoke `/vibesys:optimize <task> [--fanout N]` to start or resume the task's one long-running
  optimization: planners write hypotheses as plan files, workers try them, every commit is
  measured and judged. Its state is described by the `state` skill. Search skills are named verb-first
  (`measure`, `judge-candidate`, ...) except `state`.

Assets that only the VibeSys app uses (evaluators, profilers, the chat agent's skills) stay in
`resources/`. The reference engine repositories the serving skill cites are submodules in
`third_party/`. Skills for developers of this repository are in `.agents/skills/`. None of
them is published.

## Manifest

`.claude-plugin/marketplace.json` declares one plugin, `vibesys`, whose source is this
directory. Claude Code and Codex install from it, and `npx skills` uses it to find the skills
inside vendored packs. Its `skills` list is generated from the tree, and its `version` is set
by hand: without one, every commit would look like a plugin update.

```bash
python3 -m scripts.published_skills --write   # regenerate the skill list after adding or removing a skill
python3 -m scripts.published_skills           # what CI runs: manifest is current, every skill passes
claude plugin validate .                      # Claude Code's own check of the manifest
```

The check fails when the manifest differs from the tree, and when a skill breaks the
[Agent Skills specification](https://agentskills.io/specification) that all three tools read:
a `name` that differs from its folder or is not lowercase letters, digits and single hyphens
(at most 64 characters), a `description` over 1,024 characters, a `compatibility` over 500, a
frontmatter field the specification does not define, or a name another skill also uses. It also
fails when a domain skill names a tool that only a VibeSys run offers, such as
`submit_evaluation`, and when a skill's `references/harness/` files use a heading outside the
list above or answer different capabilities from each other. Vendored packs, which stay
byte-identical with upstream, are exempt from the tool-name rule and from the field rule: the
Neuron pack's `argument-hint` and `context` fields are Claude Code extensions that the
specification's reference validator rejects.

Bump the marketplace plugin version for every release that changes files under `skills/`
including fixes to scripts and instructions. Use a patch bump for compatible
fixes, a minor bump for new capabilities, and a major bump for incompatible contracts.
The generator preserves this manually chosen release version; CI checks the generated list,
not release intent. Without a bump, installed Claude Code and Codex caches may retain the old payload.
