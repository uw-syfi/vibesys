---
name: write-skills
description: The convention for the developer agent skills in this repository (kinds, placement, shape, content, enforcement, maintenance) and how to apply it. Use when adding, updating, splitting, reorganizing or deleting a developer agent skill, or when deciding whether a lesson belongs in a skill, AGENTS.md, the docs, or a README.
metadata:
  kind: workflow
---

# Write skills

Skills in `.agents/skills/` (`.claude/skills` is a symlink to it) guide agents
that develop VibeSys. They are not the bundled product skills, which ship to
optimization agents and follow the `add-bundled-skill` skill instead.

Use this skill, not a personal skill-maintenance skill, for developer skills.
`check_skills` enforces the rules marked checked; run
`uv run python -m scripts.check_skills` (`--survey` lists allowlisted ones too).

## Kinds

Set the kind in the frontmatter as `metadata.kind` (checked).

- **generic**: engineering judgment true in any well-run codebase
  (`software-design`, `testing`). Named as a discipline noun. It names no repo
  path, symbol, command or number (checked, for the names this repository's
  layout makes recognizable; numbers are Unenforced: they cannot be told apart
  from rule numbers and counts in prose). A `references/<language>.md` covers
  that language's idioms, not this repository's setup.
- **codebase**: how to make one kind of change here. Named verb-object
  (`change-wire-protocol`, `add-domain`, `write-vibesys-tests`). Its `##`
  sections are exactly, in order (checked): Builds on (the generic skills it
  applies), When this applies (the change kinds, described by task), Steps,
  Testing, Known failure modes, Sources of truth. Each codebase skill owns a
  disjoint set of change kinds; a task spanning two loads both.
  Unenforced: disjointness of change kinds needs judgment.
- **workflow**: a task around the code (`open-pr`, `review-pr`,
  `create-issue`, `triage-prs`, `vs-meta-opt`). Named verb-object.

Dependencies point one way: codebase skills cite generic ones, and a generic
skill never names a codebase skill (checked).

## Placement

Put each piece of knowledge in the one place that owns it.
Unenforced: placement is judgment; reviewers apply this table.

| Knowledge | Home |
| --- | --- |
| A one-line rule that is always true | `AGENTS.md`, linking to the skill or doc that explains it |
| Why the system is shaped as it is | A page under `docs/` |
| How to make one kind of change here | A codebase skill |
| How to think about a problem anywhere | A generic skill |
| What a module is and holds | A README beside the code |
| Where things are, numbers, thresholds and lists | The code, config or check that owns them; the check's failure message names the location |
| Plans, decision logs, research notes, bug history | Issues and PRs |

## Content

A skill teaches how to think about a kind of change; the code and the checks
say where things are. A skill that points at files breaks whenever the code
moves, and a stale pointer reads exactly like a live one.

- Keep: architectural principles (layers, one-way dependencies, the facade
  rule that product code is reached through `vibesys.api`, pure core and thin
  shell); library and package names (`vs-core`, `agentshim`,
  `@vibesys/run-session`); the codebase's concept names (flow, search, Role,
  provider, registry); the public entry points an agent starts from
  (`vs_agent.api`, `pnpm new:package`); stable commands
  (`./support/repoctl/repoctl test`); the identifiers of the enforcing checks;
  links to pages under `docs/` and to other skills.
- Remove: file and directory paths, `<file>:<symbol>` and `<test>::<name>`
  pointers, bare repository file names, and module paths past a package's
  `api` facade (checked: the `path-token` rule, in every skill Markdown file,
  frontmatter and fenced blocks included, and in the index); issue and pull
  request numbers (checked: `issue-number`).
- Remove internal constant, function and class names and enumerations of
  today's members (every provider, every option value); say how to find the
  current set instead ("the provider registry lists them"). Unenforced: an
  internal name cannot be told from a concept name without judgment.
- Describe triggers by task ("adding or changing an agent CLI provider"),
  never by path (checked by `path-token`, which covers the description).

## Shape

- `name` equals the directory name (checked).
- `description` says what the skill does, then "Use when ..." with the change
  kinds that should trigger it (checked: "Use when" present;
  Unenforced: trigger quality needs judgment).
- The frontmatter holds only `name`, `description` and `metadata.kind`
  (checked). Codex display metadata goes in `agents/openai.yaml`.
- Name a skill with terms the codebase itself uses, and never a term that
  collides with another repo concept: "protocol" alone is ambiguous beside
  `vs-evaluator-protocol`, so the wire-protocol skill is
  `change-wire-protocol`. Unenforced: name clarity needs human judgment.
- `SKILL.md` is at most 150 lines (checked), imperative, and says when to open
  each reference.
- A skill directory holds only `SKILL.md`, `references/`, `scripts/`,
  `agents/` and `assets/` (checked). `references/` is one level deep, each
  file at most 300 lines, and every file linked from `SKILL.md` (checked).
- `scripts/` holds only deterministic steps that a test or CI runs; a script
  no test, workflow or check group names fails (checked).
- Never invent snippets. Docs links and dotted `<package>.api` names must
  resolve (checked). Unenforced: an invented snippet cannot be spotted.
- List the skill in [README.md](../README.md) under its kind (checked).

## Enforcement

Every rule in a skill names its enforcing check or carries
`Unenforced: <reason>`. Cite a check by its stable identifier, in backticks,
never by its file: the check's own failure message carries the specifics. A
citation is one of:

- a Python test function name, or a check script name (`check_tach`);
- a namespaced client package script (`pnpm check:ts-architecture`);
- an enabled Ruff code (`TID251`) or Biome rule (`lint/security/noGlobalEval`), a Grit
  lint rule or a dependency-cruiser rule by its configured name;
- a CI check group (`python_guards`), for "CI fails if ...", or a stable
  command (`./scripts/check_lint.sh`).

In codebase skills every top-level item under Steps, Testing and Known
failure modes must hold one that exists (checked). Anywhere in a skill, a
span shaped like a test name, check script name, package script or Biome rule
must name one that exists (checked: `unresolved-check`). If a check's failure
message does not say what is wrong and how to fix it, fix the message rather
than restating the location in the skill. To add enforcement for a rule,
follow the `enforce-rules` skill.

## Maintenance

- A PR that changes an area's mechanics updates that area's skill in the same
  PR. Unenforced: no check maps code areas to skills yet.
- A bug fix in a codebase skill's area first tries to add a check through
  `enforce-rules`. If enforcement is untenable, add a Known failure modes line
  stating the bug class, marked `Unenforced: <reason>`.
- Past about 15 Known failure modes entries, look for the structural cause
  behind them and fix that instead. Unenforced: needs judgment.
- Add a skill only for a recurring change kind or bug class no skill covers.
  Unenforced: needs judgment.
- Existing violations live in the shrink-only skill-violation allowlist that
  `check_skills` reads. Never add an entry; when you fix a violation, lower or
  delete its entry in the same PR (checked: a stale count fails).

## Steps

1. Decide the home with the placement table. If it is not a skill, stop.
2. Pick the kind, then extend the skill that owns the change kind, or create
   a new skill directory with the frontmatter above.
3. Write rules with their enforcement or `Unenforced:` reason.
4. Add or update the entry in [README.md](../README.md).
5. Run `uv run python -m scripts.check_skills` and `-m scripts.check_doc_links`.
