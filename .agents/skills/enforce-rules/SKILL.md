---
name: enforce-rules
description: Enforce a rule in this repository with the strongest mechanism that holds it, from types and lint rules down to written guidance marked as unenforced. Use when adding a rule anywhere (a skill, AGENTS.md, a doc, a review comment), when a bug fix reveals a bug class that can recur, or when adding, changing or wiring a check, lint rule, graph rule or CI check group.
metadata:
  kind: codebase
---

# Enforce rules

A rule that lives only in prose holds until the first agent that did not read
it. Before writing a rule down, find the strongest mechanism that can hold it,
wire that check into CI, and leave written guidance only where enforcement is
untenable, saying so next to the rule.

## Builds on

- `software-design`: rule 7 (parse at boundaries) and the Mechanism
  checkpoint, which names the bug class a check should close.
- `testing`: how the check itself is tested.
- [write-skills](../write-skills/SKILL.md): how a skill cites its checks.

## When this applies

- Adding a rule to a skill, `AGENTS.md`, a doc under `docs/`, or a review comment.
- Fixing a bug whose Mechanism answer names a class that can recur.
- Adding or changing a check: a repository policy script, a pytest guard, an
  architecture test, a Ruff or Biome rule, a Grit lint plugin, a Python or
  TypeScript graph rule, or a CI check group.

## Steps

1. **Climb the ladder; stop at the first rung that holds the rule.** Strongest first:
   1. Types and schemas. Pydantic models reject unknown keys
      (`test_every_model_forbids_unknown_keys_or_declares_why_not` holds every
      product model to it); closed sets are `StrEnum` or `Literal`; a
      cross-process contract has one authoritative definition with generated
      downstream types (`check_contract_sot`, and the `clients_quality` group
      regenerates the protocol bindings and fails on a diff).
   2. Lint rules. Ruff, including banned imports through `TID251`; Grit
      plugins loaded by the Biome configuration; Biome's own rules.
   3. Graph checks. Python module edges are held by `check_tach`; the
      TypeScript package graph by `check_package_graph`.
   4. Architecture and quality tests, for the structure of product code and
      for repository policy and ratchets.
   5. CI check scripts, run from a check group.
   6. Written guidance, only when every rung above is untenable (step 2).
2. **Call enforcement untenable only for one of three reasons:** the rule needs
   human judgment; a check would breed false positives and so suppressions; or
   the check would cost more CI time, or be flakier, than the bugs it prevents.
   Then write the rule with `Unenforced: <reason>`, so readers and the next
   audit see that nothing guards it. `check_skills` fails a rule item in a
   codebase skill that has neither a check nor the marker.
3. **Wire the check where every relevant change runs it.** A Python policy
   script joins the `python_quality` check group; a pytest guard runs in
   `python_guards`; a TypeScript check joins `clients_quality`. Paths that the
   CI component selector ignores (such as the agent skills) select no job, so
   a check over them goes in the always-on `doc-checks` job, stdlib-only.
   Stay inside the CI wall-time budget that CI enforces: a scan of the whole
   repository should take seconds. CI fails if a check group fails;
   `./support/repoctl/repoctl run-checks --group <name>` runs one group, and
   the group definitions say what each runs.
4. **Ratchet existing violations; never weaken the rule to pass today.** Record
   each existing violation in a shrink-only allowlist that the check reads, as
   `check_purity`, `check_file_length`, `check_test_isolation` and
   `check_skills` do: a new or grown violation fails. A per-site opt-out is a
   lint suppression, allowed only when every lint-compliant alternative would
   make the design more hacky, with the alternatives listed in the source
   rationale and a waiver ID (`check_lint_waivers`; Ruff's `RUF100` rejects a
   suppression that no longer suppresses anything). The waiver format is in
   [coding-best-practices.md](../../../docs/contributing/coding-best-practices.md),
   "Ratchets".
5. **Make every failure message say what to do.** Name the offending path
   (with its line) or key, the rule it breaks, and the fix, as `check_skills`
   does. A skill cites a check by identifier rather than location, so the
   message is the only place the specifics live; if it does not say what is
   wrong and how to fix it, improve the message instead of naming the location
   in prose. Unenforced: whether a message is actionable needs a reader; the
   check's tests should at least assert that the offending path appears in its
   output.
6. **Cite the enforcement beside the rule.** Wherever the rule is written,
   name the check that holds it by identifier, never by file: a test function
   name, a check script name, a lint code such as `TID251`, or a
   CI check group such as `python_guards`; or write `Unenforced: <reason>`. In codebase
   skills `check_skills` checks this (see
   [write-skills](../write-skills/SKILL.md), "Enforcement"). Unenforced: prose
   outside skills has no machine-readable rule structure to check.

## Testing

- Drive the check through its public functions over synthetic trees in a
  temporary directory, with one violating and one compliant input per rule, so
  a check that matches nothing cannot pass. `test_a_codebase_rule_item_needs_a_resolving_check_or_the_unenforced_mark`
  pairs its cases this way.
- Prefer property tests over the input space (generated names, paths, counts).
  Unenforced: choosing the input space needs judgment.
- Pass the tracked file list in instead of calling Git from the test; a
  deterministic-tier test runs no subprocess
  (`test_deterministic_tests_use_no_real_time_threads_processes_or_sockets`).
- Show that the check fails at the merge base for the bug it targets: run it
  on the pre-fix tree and paste the failure in the PR's Verification section.
  Unenforced: CI runs only the head; the reviewer checks the claim.

## Known failure modes

- A check gated on a hard-coded file list lets new files escape, so citations
  in every other doc rot unchecked. Derive the scope from the tracked tree, as
  `check_skills` does for the agent skills. Unenforced: needs a reader to
  recognize a hard-coded scope.
- A check that strips the syntax it should inspect sees nothing there:
  `check_doc_links` removes inline code spans before matching links, so a path
  written in backticks is invisible to it, and `check_doc_citations` exists to
  read exactly those spans. Unenforced: the blind spot is specific to each
  check's parser.
- A rule described as enforced with no check behind it: the per-layer CPU
  budgets in [testing-map.md](../../../docs/contributing/testing-map.md) read
  as enforced, but nothing sums them. Unenforced: telling a stated guarantee
  from a description needs a reader; ask "which check fails?" in review.
- A check wired into a job the change never selects: the component selector
  ignores the agent skills, so a skills-only PR skips the Python quality
  group, and `check_skills` also runs in the always-on docs job for that
  reason. Unenforced: needs a reader to spot a job the change skips.
- An allowlist that fails only on growth goes slack: fixing one violation makes
  room for an unrelated new one. Fail on any change of a recorded count, as
  `check_skills` does.

## Sources of truth

- The CI check groups and their commands; `./support/repoctl/repoctl test --dry-run`
  lists the groups a change selects.
- The Ruff, Biome and `tach.toml` configuration: the lint and graph rules,
  with their thresholds.
- [coding-best-practices.md](../../../docs/contributing/coding-best-practices.md):
  size limits, ratchets, and the lint-waiver format.
- [AGENTS.md](../../../AGENTS.md): the lint-suppression rule.
