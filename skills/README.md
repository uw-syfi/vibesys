# Skills

Everything in this directory is published as eight plugins and as a set of Agent Skills, and
nothing outside it is. Claude Code and Codex copy a plugin's source directory when they
install it, so a skill that lives elsewhere in the repository cannot reach a user.

## Install

Pick the tool you run. Each reads the same skills.

| Tool | Install | Skill names |
| --- | --- | --- |
| Claude Code | `claude plugin marketplace add uw-syfi/vibesys`, then `claude plugin install <plugin>@vibesys` | `<plugin>:optimize` |
| Codex | `codex plugin marketplace add uw-syfi/vibesys`, then `codex plugin add <plugin>@vibesys` | `<plugin>:optimize` |
| Any agent, through [`npx skills`](https://github.com/vercel-labs/skills) | `npx skills add uw-syfi/vibesys/skills --skill '*' --agent <agent>` | `optimize` |

Every plugin installs from the one source directory and differs only by the skills it lists:

| Plugin | Skills |
| --- | --- |
| `vibesys` | the optimization loop (`search/`): install it first |
| `vibesys-llm-serving` | `serving-systems` |
| `vibesys-microservices` | `microservice-optimization` and the Tokio `dial9-*` skills |
| `vibesys-database` | the Tokio `dial9-*` skills, which are listed in both `vibesys-database` and `vibesys-microservices` |
| `vibesys-kernels-cuda` | `kernel-correctness`, `kernel-performance`, `kernel-ncu-analysis`, `kernel-timeline-analysis` |
| `vibesys-kernels-rocm` | `kernel-correctness`, `kernel-performance`, `kernel-rocprof-analysis` |
| `vibesys-kernels-trainium` | `kernel-correctness`, `kernel-performance`, the Neuron NKI pack |
| `vibesys-cli-tools` | `cli-tool-optimization` |

Inside a Claude Code session (2.1.275 or later) each plugin also installs in one step, with no separate
`marketplace add`:

```text
/plugin install vibesys --marketplace uw-syfi/vibesys
/plugin install vibesys-llm-serving --marketplace uw-syfi/vibesys
/plugin install vibesys-microservices --marketplace uw-syfi/vibesys
/plugin install vibesys-database --marketplace uw-syfi/vibesys
/plugin install vibesys-kernels-cuda --marketplace uw-syfi/vibesys
/plugin install vibesys-kernels-rocm --marketplace uw-syfi/vibesys
/plugin install vibesys-kernels-trainium --marketplace uw-syfi/vibesys
/plugin install vibesys-cli-tools --marketplace uw-syfi/vibesys
```

Install `vibesys` plus the domain plugin the task needs, chosen by the task's `[agent] domain` and
`backends` (a Trainium LLM-serving task needs `vibesys-llm-serving` and `vibesys-kernels-trainium`);
`optimize` stops with the install commands when one is missing. Skill names carry the plugin
name, for example `vibesys-kernels-cuda:kernel-performance`. Codex reads the
`.claude-plugin/marketplace.json` described below, so the repository has one manifest for both.
`npx skills` copies each skill into the chosen agent's skills directory (`.agents/skills/` for
Codex; add `--global` for the user-level one), with no plugin prefix: read
`vibesys:measure` in a skill as the skill `measure`. It installs every skill at once, so the
domain plugins do not apply. Give it the `/skills` path, as above, because the repository also
holds developer skills in `.agents/skills/` that `npx skills add uw-syfi/vibesys` would offer too.
`--list` shows what a command would install.

The optimize loop needs a harness with subagents and background commands, git, and `vibesys-cli`,
the standalone binary that measures, compares and promotes commits and stores the plans.
The `setup` skill, which `create-task` and `optimize` run first, reuses an existing CLI or asks
your permission to install it from the official GitHub releases, checks that its version is one
the skills support, and upgrades it only on request and only to a supported release. It selects
an absolute `VIBESYS_CLI` path for the session without editing your shell profile. The `commit`
skill's message checker also needs Python 3.10 or newer. Nothing needs the VibeSys Python package.

| Harness | Domain skills | `optimize` loop |
| --- | --- | --- |
| Claude Code | supported | supported |
| Codex | supported | harness mechanisms written from Codex's docs and source; **untested end to end** |
| VibeSys orchestrator (Claude Code or Codex agents, spawned through its tools) | supported | `vibesys-orchestrator.md`, selected when `VIBESYS_RUN` is set; **untested end to end** |
| Other agents (`npx skills`) | supported | needs a harness file for that agent, as described below |

## Harness neutrality

The search skills are harness-neutral. A harness is the program running the agent loop (Claude
Code, Codex, ...), not the model. The skills describe abstract capabilities; a skill that
needs any carries `references/harness/<harness>.md`, and its `SKILL.md` points there in one
sentence. Skills with no harness need have no such folder. The plugin packaging
(`.claude-plugin/`) is Claude Code's delivery format, which Codex and `npx skills` also read, and
is the only Claude-specific part outside those folders.

The VibeSys orchestrator is a harness in this sense too: it hosts the lead and starts every subagent through MCP tools, on either Claude Code or Codex, so its file replaces the per-harness choice whenever `VIBESYS_RUN` is set.

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

- `domains/`: domain skills for kernels, LLM serving, microservices, Tokio services, native
  command-line tools and Neuron. Both the plugin and VibeSys runs use them: a run reads only this tree, and the
  sidecar in it decides which skills a run installs for its backend and domain
  ([skill metadata](../docs/contributing/skill-metadata.md)).
- `search/`: the optimization loop for the plugin alone. VibeSys runs never install it,
  because they read only `domains/`. Invoke `/vibesys:optimize <task> [--fanout N]` to start or resume the task's one long-running
  optimization: planners store hypotheses as plans, workers try them, every commit is
  measured and judged, all through `vibesys-cli`. Its state is described by the `state` skill, and every commit follows the
  `commit` skill's enforced message schema. `setup` finds, installs, upgrades and
  version-checks `vibesys-cli`; the oldest version the skills support is in its
  `assets/min-cli-version`. `create-task` turns a description of what to
  optimize into a task and validates it with real jobs before a run starts. Search skills are named verb-first
  (`measure`, `judge-candidate`, ...) except `state`.

Assets that only the VibeSys app uses (evaluators, profilers, the chat agent's skills) stay in
`resources/`. The reference engine repositories the serving skill cites are submodules in
`third_party/`. Skills for developers of this repository are in `.agents/skills/`. None of
them is published.

## Manifest

`.claude-plugin/marketplace.json` declares the eight plugins above, each with the source `./skills`.
Claude Code and Codex install from it, and `npx skills` uses it to find the skills inside vendored
packs. Each plugin's `skills` list is generated from the tree and the plugin table in
`scripts/published_skills/plugins.py`: a skill belongs to a plugin when its folder is, or lies
under, one of the plugin's folders. A skill may be listed by several plugins (the kernel skills
that no backend owns are listed by all three kernels plugins), but every skill must belong to at
least one, and every folder in the table must match a skill. A `name`, `description`, `version`
and `strict` are written by hand: without a `version`, every commit would look like a plugin
update.

```bash
python3 -m scripts.published_skills --write   # regenerate the skill lists after adding or removing a skill
python3 -m scripts.published_skills           # what CI runs: manifest is current, every skill passes
claude plugin validate .                      # Claude Code's own check of the manifest
```

The check fails when the manifest differs from the tree, when its plugin entries are not exactly
the table's, and when a skill breaks the
[Agent Skills specification](https://agentskills.io/specification) that all three tools read:
a `name` that differs from its folder or is not lowercase letters, digits and single hyphens
(at most 64 characters), a `description` over 1,024 characters, a `compatibility` over 500, a
frontmatter field the specification does not define, or a name another skill also uses. It also
fails when a domain skill names a tool that only a VibeSys run offers, such as
`submit_evaluation`, and when a skill's `references/harness/` files use a heading outside the
list above or answer different capabilities from each other. It fails when a search skill names a
`references/`, `scripts/` or `assets/` path of its own or a sibling's that does not exist, and when
the `setup` skill's minimum CLI version is not a plain `MAJOR.MINOR.PATCH`. Vendored packs, which stay
byte-identical with upstream, are exempt from the tool-name rule and from the field rule: the
Neuron pack's `argument-hint` and `context` fields are Claude Code extensions that the
specification's reference validator rejects.

Bump the version of each plugin whose listed skills changed, and only that plugin, for every
release that changes files under `skills/`, including fixes to scripts and instructions. A change
to a skill listed by several plugins bumps each of them. Use a patch bump for compatible
fixes, a minor bump for new capabilities, and a major bump for incompatible contracts.
The generator preserves these manually chosen versions; CI checks the generated lists,
not release intent. Without a bump, installed Claude Code and Codex caches may retain the old payload.
