---
name: add-bundled-skill
description: Add or change a bundled Agent Skill, the skills that ship inside the VibeSys wheel and are installed for optimization agents during runs, including its sidecar (backend- and domain-scoped selection rules), its per-backend platform guidance, and the update script that re-vendors an upstream skill pack. Use when adding, editing, re-vendoring or scoping a bundled skill, or when changing how bundled skills are selected, validated, packaged or copied into an agent workspace.
metadata:
  kind: codebase
---

# Add a bundled skill

Bundled skills are the Agent Skills that ship in the VibeSys wheel and are
copied into each optimization agent's workspace when a run starts. They are
not the developer skills in `.agents/skills/`, which guide agents that change
VibeSys and follow the `write-skills` skill. The bundled-skill contract is
described in [skill-metadata.md](../../../docs/contributing/skill-metadata.md).

A run picks its skills in three stages. Discovery finds every directory with a
`SKILL.md` under the candidate roots: the bundled tree by default, changed by
`--skills-dir`, `--extra-skills` and `--no-skills`
([cli-flags.md](../../../docs/cli-flags.md)). Selection keeps a skill when the
sidecar rule that matches it allows the run's compute backend and domain.
Materialization copies each kept skill into the workspace, dropping other
backends' platform directories and vendored checkouts. The bundled tree is the
domain-skills part of the repository's published skills tree, and a run never
reads the rest of it. The wheel build copies the bundled tree into the package
as its own staged tree; a checkout wins at run time. Reference repositories a
skill cites are submodules outside every skill, so they never reach the wheel
or a workspace.

## Builds on

- `software-design`: routing metadata is parsed at the boundary, with errors
  naming the offending path.
- `testing`: tests build synthetic skill trees and call public functions.

## When this applies

- Adding, editing or removing a bundled skill.
- Writing or changing a sidecar, or a skill's per-backend platform guidance.
- Vendoring an upstream skill pack, or changing its update script.
- Changing skill selection, validation or copying in the run and runtime
  layers, or wheel staging of the bundled tree.
- Adding a domain or a compute backend changes which skills load: load the
  `add-domain` or `add-compute-backend` skill for that change, and this one for
  the skills it affects.

## Steps

1. **Write a standard `SKILL.md`.** It needs YAML frontmatter, a `name` equal
   to the directory name, and a non-empty `description`. Put no VibeSys fields
   in the frontmatter: routing goes in the sidecar, so vendored skills stay
   byte-identical to upstream. `test_missing_skill_frontmatter_is_invalid`
   fails if discovery accepts missing or malformed frontmatter, and
   `test_catalog_requires_frontmatter_name_to_match_directory` fails if the
   run-start catalog accepts a mismatched name. Unenforced: an extra
   frontmatter key is valid YAML, and agent harnesses ignore unknown keys.
2. **Scope it with a sidecar rule, or leave it global.** A skill that no rule
   matches loads for every backend and domain. To restrict it, add a rule with
   a directory path and optional `backends` and `domains` lists. First-party
   top-level skills share one sidecar at the root of the bundled tree; a
   vendored pack keeps its own beside its update script. Rule semantics and
   every validation error are in [sidecar-rules.md](references/sidecar-rules.md).
   `test_invalid_sidecar_metadata_fails_with_sidecar_path` fails if a malformed
   sidecar is accepted or its error omits the sidecar's path.
3. **Pin the scope of a first-party skill in the metadata test.** Add its
   expected `backends` and `domains` to the test that validates the whole
   bundled tree, `test_all_repository_skill_metadata_is_valid`. Unenforced: a
   skill left out of it still validates, with its scope unasserted.
4. **Put per-backend guidance in a platform directory per backend.** Inside
   the skill's references, each platform directory is named for a
   `ComputeBackend` value and holds every file of the platform skeleton that
   skill selection declares.
   `test_validate_skill_tree_enforces_platform_layout` fails otherwise (it runs
   in CI, not at run start). Materialization prunes every other backend's
   directory, so the agent cannot read a foreign platform's guidance;
   `test_reviewer_alias_to_pruned_platform_is_rejected_and_removed` fails if
   one is exposed. Keep portable material elsewhere in the references and never
   link from it into one platform's files, since the link dangles on every
   other backend. `test_portable_references_never_link_into_a_platform_dir`
   covers only the largest bundled skill; Unenforced for other skills: no
   generic scan exists.
5. **Vendor upstream packs through a wrapper directory.** The wrapper holds
   the update script, a README (upstream URL, pinned commit, what is and is
   not vendored), the upstream license and notice, the sidecar, and a subtree
   of skills that the update script deletes and recreates. With the sidecar
   outside that subtree, a re-vendor keeps routing. Copy only the skills you
   need, pin a commit, and make any fix-ups inside the update script rather than
   by hand; an existing vendored pack is the model. Unenforced: no check runs
   an update script or compares the vendored tree with upstream, since that
   needs network access.
6. **Cite skill files from prompts and objectives by checkout path.** Write
   the checkout-relative path into the bundled tree; the run rewrites it to the
   workspace path and fails before the first agent turn if the file is
   missing, excluded for the run's backend, or inside a vendored checkout.
   `test_unavailable_source_resources_fail_before_an_agent_turn` fails if it
   does not. Packaged prompt templates are also checked against the built
   wheel: `test_every_packaged_prompt_renders_without_checkout_fallback`.

## Testing

- Run the skills wiring tests after any change to a bundled skill: they
  validate every bundled skill and sidecar, check platform layout and links,
  and check selection per backend and domain.
  `test_kernel_skills_follow_domain_and_backend` is the model for testing a new
  scoped skill. Unenforced: knowing to run them is judgment; CI runs them.
- Test selection or validation code changes with one violating and one valid
  synthetic tree per rule, like `test_invalid_sidecar_metadata_fails_with_sidecar_path`.
- A change to which files reach the wheel needs a case beside
  `test_stage_resources_copies_trees_and_drops_vendored_checkouts`;
  `test_every_packaged_prompt_renders_without_checkout_fallback` builds the
  real wheel and renders against it.

## Known failure modes

- A skill subdirectory named like a build output ships in a checkout run but
  vanishes from the wheel: staging drops every excluded name at any depth
  (`test_stage_resources_copies_trees_and_drops_vendored_checkouts`).
  Unenforced: no test scans the bundled tree for those names.
- Two equally deep rules that match one skill with different scopes fail
  every run that discovers it instead of picking one
  (`test_conflicting_same_specificity_rules_fail`). Give the narrower rule a
  deeper `path`.
- Skills and sidecars under a directory whose name starts with `.` are
  silently skipped by discovery
  (`test_discovery_ignores_hidden_skill_directories`).
- Files a skill reaches through symlinks can disappear from the workspace:
  materialization removes links that resolve outside the copied skill or into
  a pruned directory (`test_materialization_removes_invalid_links`). Vendor
  with dereferenced copies, as the existing vendored pack's update script does.
- Python inside a skill gets no Ruff or ty check, because the project config
  excludes the bundled tree as vendored. Unenforced: deliberate; review holds
  first-party code.

## Sources of truth

- The skill-selection logic in the orchestration layer: sidecar parsing, rule
  precedence, platform layout and pruning, and citation rewriting.
- The run layer's skill-source handling: discovery plus selection for a run,
  and whole-tree validation.
- The skill catalog in `vs-runtime`: frontmatter parsing, discovery, and the
  catalog that agents see.
- The `ComputeBackend` and `DomainName` enums: the
  allowed `backends` and `domains` values.
- The wheel staging step: which resource trees and names reach the wheel.
- The authoring guide beside the largest bundled skill: which of its
  references are portable.
