---
name: write-skills
description: The convention for agent skills in .agents/skills/ (kinds, placement, shape, enforcement, maintenance) and how to apply it. Use when adding, updating, splitting, reorganizing or deleting a skill in .agents/skills/ or .claude/skills/, or when deciding whether a lesson belongs in a skill, AGENTS.md, docs/, or a README.
metadata:
  kind: workflow
---

# Write skills

Skills in `.agents/skills/` (`.claude/skills` is a symlink to it) guide agents
that develop VibeSys. They are not the product skills in `resources/skills/`,
which ship to optimization agents and follow
[skill-metadata.md](../../../docs/contributing/skill-metadata.md) instead.

Use this skill, not a personal skill-maintenance skill, for anything under
`.agents/skills/`. `scripts/check_skills.py` checks the rules marked as checked
below; run `uv run python -m scripts.check_skills` after every change, and
`--survey` to list allowlisted violations too.

## Kinds

Set the kind in the frontmatter as `metadata.kind` (checked).

- **generic**: engineering judgment true in any well-run codebase
  (`software-design`, `testing`). Named as a discipline noun. It names no repo
  path, symbol, command or number (checked, for the names this repository's
  layout makes recognizable; numbers are Unenforced: they cannot be told apart
  from rule numbers and counts in prose). A `references/<language>.md` covers
  that language's idioms, not this repository's setup.
- **codebase**: how to make one kind of change here, concretely. Named
  verb-object (`change-wire-protocol`, `add-domain`, `write-vibesys-tests`).
  Its `##` sections are exactly, in order (checked): Builds on (the generic
  skills it applies), When this applies (change kinds and paths), Steps,
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
| Why the system is shaped as it is | `docs/` |
| How to make one kind of change here | A codebase skill |
| How to think about a problem anywhere | A generic skill |
| What a module is and holds | A README beside the code |
| Numbers, thresholds and lists | The config or code that enforces them; skills link to it and never restate it |
| Plans, decision logs, research notes | Issues and PRs |

## Shape

- `name` equals the directory name (checked).
- `description` says what the skill does, then "Use when ..." with the change
  kinds and file areas that should trigger it (checked: "Use when" present;
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
- Examples cite real code as `path:symbol`, never invented snippets. Every
  citation, dotted package name and repo path must resolve (checked).
  Unenforced: an invented snippet in a fenced block cannot be told apart from
  a real one.
- List the skill in [README.md](../README.md) under its kind (checked).

## Enforcement

Every rule in a skill names its enforcing check (a test, a script, a lint rule
code, a graph rule) or carries `Unenforced: <reason>`. In codebase skills this
is checked for every top-level item under Steps, Testing and Known failure
modes: the item must contain, in backticks, a tracked check (a file under a
`tests` directory, any test file named by its runner's convention such as
`test_*.py`, `*_test.py`, `*.test.ts` or `*.spec.ts`, anything under
`scripts/`, `clients/tools/` or `.github/workflows/`, or `tach.toml`,
`.repoctl/checks.toml` or `clients/.dependency-cruiser.mjs`), alone or as
`path:symbol`, or a Ruff or Biome rule the configuration enables. To add enforcement for a rule, follow the `enforce-rules` skill; do not
restate it here.

## Maintenance

- A PR that changes an area's mechanics updates that area's skill in the same
  PR. Unenforced: no check maps code areas to skills yet.
- A bug fix in a codebase skill's area first tries to add a check through
  `enforce-rules`. If enforcement is untenable, add a Known failure modes line
  naming the PR number and marked `Unenforced: <reason>`.
- Past about 15 Known failure modes entries, look for the structural cause
  behind them and fix that instead. Unenforced: needs judgment.
- Add a new skill only for a recurring kind of change or bug class that no
  skill covers. Unenforced: needs judgment.
- Existing violations live in the shrink-only allowlist
  `scripts/skill_violations.json`. Never add an entry; when you fix a
  violation, lower or delete its entry in the same PR (checked: any count
  change fails until the record matches).

## Steps

1. Decide the home with the placement table. If it is not a skill, stop.
2. Pick the kind, then extend the skill that owns the change kind, or create
   `.agents/skills/<name>/SKILL.md` with the frontmatter above.
3. Write rules with their enforcement or `Unenforced:` reason.
4. Add or update the entry in [README.md](../README.md).
5. Run `uv run python -m scripts.check_skills` and `python3 scripts/check_doc_links.py`.
