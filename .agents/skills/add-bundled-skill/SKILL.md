---
name: add-bundled-skill
description: Add or change a bundled Agent Skill in resources/skills/, the skills that ship inside the VibeSys wheel and are installed for optimization agents during runs, including its .vibesys.toml sidecar (backend- and domain-scoped selection rules), its references/platforms/<backend>/ tree, and the update.sh that re-vendors an upstream skill pack. Use when adding, editing, re-vendoring or scoping anything under resources/skills/, or when changing how bundled skills are selected, validated, packaged or copied into an agent workspace.
metadata:
  kind: codebase
---

# Add a bundled skill

Bundled skills are the Agent Skills under `resources/skills/`. They ship in
the VibeSys wheel and are copied into each optimization agent's workspace, at
`.agents/skills/<name>/` inside that workspace, when a run starts. They are
not the developer skills in this repository's own `.agents/skills/`, which
guide agents that change VibeSys and follow the `write-skills` skill.

A run picks its skills in three stages. Discovery finds every directory with a
`SKILL.md` under the candidate roots: the bundled tree by default, changed by
`--skills-dir`, `--extra-skills` and `--no-skills`
([cli-flags.md](../../../docs/cli-flags.md)). Selection keeps a skill when the
`.vibesys.toml` sidecar rule that matches it allows the run's compute backend
and domain. Materialization copies each kept skill into the workspace,
dropping other backends' platform directories and vendored checkouts. The
wheel build copies the tree into the package
(`packaging/resources_packaging.py:stage_resources`); a checkout wins at run
time.

## Builds on

- `software-design`: routing metadata is parsed at the boundary, with errors
  naming the offending path.
- `testing`: tests build synthetic skill trees and call public functions.

## When this applies

- Adding, editing or removing a skill under `resources/skills/`.
- Writing or changing a `.vibesys.toml` sidecar, or a skill's
  `references/platforms/<backend>/` tree.
- Vendoring an upstream skill pack, or changing its `update.sh`.
- Changing selection, validation or copying in
  `src/vibesys/orchestration/skill_selection.py`,
  `src/vibesys/run/skill_sources.py`,
  `libs/vs-runtime/src/vs_runtime/_skills.py`, or wheel staging in
  `packaging/resources_packaging.py`.
- Adding a domain or a compute backend changes which skills load: load the
  `add-domain` or `add-compute-backend` skill for that change, and this one for
  the skills it affects.

## Steps

1. **Write a standard `SKILL.md`.** It needs YAML frontmatter between `---`
   lines, a `name` equal to the directory name, and a non-empty `description`.
   Discovery rejects missing or malformed frontmatter
   (`tests/entrypoints/test_skills_wiring.py:test_missing_skill_frontmatter_is_invalid`);
   the run-start catalog rejects a mismatched name or an empty description
   (`libs/vs-runtime/tests/test_skill_catalog.py`). Put no VibeSys fields in
   the frontmatter: routing goes in the sidecar, so vendored skills stay
   byte-identical to upstream. Unenforced: an extra frontmatter key is valid
   YAML, and agent harnesses ignore keys they do not know.
2. **Scope it with a sidecar rule, or leave it global.** A skill that no rule
   matches loads for every backend and domain. To restrict it, add a
   `[[rule]]` with a `path` and optional `backends` and `domains` lists.
   First-party top-level skills share `resources/skills/.vibesys.toml`; a
   vendored pack keeps its own beside `update.sh`. Rule semantics and every
   validation error are in [sidecar-rules.md](references/sidecar-rules.md);
   validation is tested by
   `tests/entrypoints/test_skills_wiring.py:test_invalid_sidecar_metadata_fails_with_sidecar_path`.
3. **Pin the scope of a repository skill in a test.** Add its expected
   `backends` and `domains` to
   `tests/entrypoints/test_skills_wiring.py:test_all_repository_skill_metadata_is_valid`,
   which validates the whole bundled tree. Unenforced: a skill left out of
   it still validates, with its scope unasserted.
4. **Put per-backend guidance under `references/platforms/<backend>/`.** Each
   directory name must be a `vibesys.constants.ComputeBackend` value, and each
   directory must hold every file in
   `src/vibesys/orchestration/skill_selection.py:PLATFORM_SKELETON`
   (`tests/entrypoints/test_skills_wiring.py:test_validate_skill_tree_enforces_platform_layout`;
   this runs in CI, not at run start). Materialization prunes every other
   backend's directory, so the agent cannot read a foreign platform's
   guidance (`tests/vibesys/run/test_materialized_resource_links.py`). Keep
   portable material elsewhere in `references/` and never link from it into
   one platform's files, since the link dangles on every other backend
   (`tests/entrypoints/test_skills_wiring.py:test_portable_references_never_link_into_a_platform_dir`
   scans `serving-systems` only; Unenforced for other skills).
5. **Vendor upstream packs through a wrapper directory.** Create
   `resources/skills/<pack>/` holding `update.sh`, a `README.md` (upstream
   URL, pinned commit, what is and is not vendored), the upstream license and
   notice, the sidecar, and a `skills/` subtree that `update.sh` deletes and
   recreates. With the sidecar outside `skills/`, a re-vendor keeps routing.
   Copy only the skills you need, pin a commit, and make any fix-ups inside
   `update.sh` rather than by hand, as `resources/skills/dial9-tokio/update.sh`
   does. Unenforced: no check runs `update.sh` or compares the vendored tree
   with upstream, since that needs network access.
6. **Cite skill files from prompts and objectives by checkout path.** Write
   `resources/skills/<skill>/<file>`; the run rewrites it to the workspace
   path and fails before the first agent turn if the file is missing,
   excluded for the run's backend, or inside a vendored checkout
   (`tests/vibesys/run/test_agent_resource_paths.py`). Packaged prompt
   templates are also checked against the built wheel
   (`tests/architecture/test_prompt_resource_contract.py`).

## Testing

- Run `tests/entrypoints/test_skills_wiring.py` after any change under
  `resources/skills/`: it validates every bundled skill and sidecar, checks
  platform layout and links, and checks selection per backend and domain.
  `tests/entrypoints/test_skills_wiring.py:test_kernel_skills_follow_domain_and_backend`
  is the model for testing a new scoped skill.
- Test selection or validation code changes with one violating and one valid
  synthetic tree per rule, like the cases of
  `tests/entrypoints/test_skills_wiring.py:test_invalid_sidecar_metadata_fails_with_sidecar_path`.
- A change to which files reach the wheel needs a case in
  `tests/packaging/test_resources_packaging.py`; the real wheel is built and
  rendered against in `tests/architecture/test_prompt_resource_contract.py`.

## Known failure modes

- A skill subdirectory named like a build output ships in a checkout run but
  vanishes from the wheel: staging drops every name in
  `packaging/resources_packaging.py:EXCLUDED_NAMES` at any depth
  (`tests/packaging/test_resources_packaging.py:test_stage_resources_copies_trees_and_drops_vendored_checkouts`).
  Unenforced: no test scans `resources/skills/` for those names.
- Two equally deep rules that match one skill with different scopes fail
  every run that discovers it instead of picking one
  (`tests/entrypoints/test_skills_wiring.py:test_conflicting_same_specificity_rules_fail`).
  Give the narrower rule a deeper `path`.
- Skills and sidecars under a directory whose name starts with `.` are
  silently skipped by discovery
  (`tests/entrypoints/test_skills_wiring.py:test_discovery_ignores_hidden_skill_directories`).
- Files a skill reaches through symlinks can disappear from the workspace:
  materialization removes links that resolve outside the copied skill or into
  a pruned directory (`tests/vibesys/run/test_materialized_resource_links.py`).
  Vendor with dereferenced copies, as `resources/skills/dial9-tokio/update.sh`
  does.
- Python inside a skill gets no Ruff or ty check: `pyproject.toml` excludes
  the tree as vendored. Unenforced: deliberate; review holds first-party code.

## Sources of truth

- `src/vibesys/orchestration/skill_selection.py`: sidecar parsing, rule
  precedence, platform layout and pruning, and citation rewriting.
- `src/vibesys/run/skill_sources.py`: discovery plus selection for a run, and
  whole-tree validation.
- `libs/vs-runtime/src/vs_runtime/_skills.py`: frontmatter parsing, discovery,
  and the skill catalog that agents see.
- `vibesys.constants.ComputeBackend` and `vibesys.constants.DomainName`: the
  allowed `backends` and `domains` values.
- `packaging/resources_packaging.py`: which resource trees and names reach the
  wheel.
- `resources/skills/serving-systems/CLAUDE.md`: the authoring guide for the
  largest bundled skill, including which references are portable.
