---
name: add-domain
description: Add an optimization domain (a problem space such as llm-serving, database or kernel-writing) as a set of coordinated edits, with a completeness test that names any piece left out. Use when adding or renaming a domain, adding a modality to one, adding a field to the domain definition, changing the domain registry or the domain name enum, or adding role prompt files to a domain.
metadata:
  kind: codebase
---

# Add a domain

A domain tells the agents what kind of system they optimize and what "good"
means there; an input bundle picks one with `[agent].domain`. It is four pieces
that must agree: a member of the domain name enum (the hyphenated name bundles
use), a domain package exporting a `DomainDefinition`, an entry in the domain
registry, and Markdown role files in the package's prompt directory. The CLI
choices and skill-metadata validation derive from the enum.

## Builds on

- `software-design`: rule 8 (one source of truth). A fact about a domain goes
  on `DomainDefinition`, not into an `if domain is ...` branch in a caller.
- `testing`: completeness as a property over the enums and the tree.
- `write-agent-prompt`: the role files' prose.

## When this applies

- A new or renamed domain, a new modality, a new `DomainDefinition` field.
- Role prompt files, or the domain registry and its enum.

## Steps

1. **Add the enum member, the package and the registry entry; let the test list
   what is missing.** Copy the generic domain's package (underscores where the
   name has hyphens), set its `name`, and register it. `test_every_domain_name_is_registered_under_its_own_name`
   and `test_every_domain_package_is_registered` fail, naming the domain, until
   the pieces agree.
2. **Write role files only where the domain has something to say.** A role file
   is named after a domain role; a missing one injects nothing, and the
   single-agent file defaults to implementer plus judge. Declare modalities in
   `modalities` and `default_modality`, with partials in a per-modality prompt
   directory. The variables role files may branch on are in
   [domains.md](../../../docs/contributing/domains.md).
   `test_prompt_dir_holds_only_role_files_docs_and_modalities` and
   `test_declared_modalities_match_their_prompt_directories` reject any other
   file name and any undeclared modality directory, which the renderer would
   silently ignore.
3. **Decide which profilers fit.** A domain that no profiler definition lists
   gets only `none` (see `add-profiler`).
   `test_profiler_auto_resolution_exhaustive` covers every domain; teach its
   expected-value helper any new `auto` rule.
4. **Record the prompt snapshots.** `test_domain_role_prompt_bytes_are_stable`
   fails for a domain it has no hash for. Rerun it with
   `UPDATE_PROMPT_SNAPSHOTS=1`, read the rendered sections, then commit the hashes.
5. **Fence distinctive vocabulary** out of neutral packs by adding a leak check
   for the new domain; `test_domain_specific_keywords_do_not_leak_to_vetted_domains`
   runs every one.
6. **Scope domain-only product skills** with a `domains` rule in their
   metadata sidecar (the `add-bundled-skill` skill);
   `test_all_repository_skill_metadata_is_valid` validates every sidecar.
7. **Update the prose list** of domains in
   [domains.md](../../../docs/contributing/domains.md).
   Unenforced: a prose restatement of the enum that no check compares.

## Testing

- `test_every_role_prompt_carries_its_domains_section_and_no_other_domains_text`
  checks, for every domain and strategy, that each role prompt carries the
  domain's sections and no other domain's text.
- Render a role file's run-dependent branches both ways (a gate present only
  with a benchmark command), as `test_render_database_judge_omits_commands_when_absent`
  does for the database domain.
- Run the domain test directory and the profiler tests. Run only the checks
  the change can affect; CI runs the rest. Unenforced: which checks a change
  can affect is judgment.

## Known failure modes

- Domain text outside the pack: strategy templates that name a domain, and
  modality text kept in shared templates;
  `test_every_role_prompt_carries_its_domains_section_and_no_other_domains_text`.
- A domain fact hard-coded in a caller: a CLI default that restates a domain's
  default modality instead of reading it from the definition. Unenforced:
  telling a policy branch on the domain from a misplaced fact needs judgment.

## Sources of truth

- The domain registry, the `DomainDefinition` and the role renderer, reached
  through `vibesys.api`.
- [domains.md](../../../docs/contributing/domains.md): what a domain is, and
  the role-file variables.
