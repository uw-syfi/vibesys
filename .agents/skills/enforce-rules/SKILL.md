---
name: enforce-rules
description: Enforce a rule in this repository with the strongest mechanism that holds it, from types and lint rules down to written guidance marked as unenforced. Use when adding a rule anywhere (a skill, AGENTS.md, a doc, a review comment), when a bug fix reveals a bug class that can recur, or when adding, changing or wiring a check in scripts/, tests/quality/, tests/architecture/, clients/tools/, tach.toml, the Ruff or Biome configuration, or .repoctl/checks.toml.
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

## When this applies

- Adding a rule to a skill, `AGENTS.md`, a doc under `docs/`, or a review comment.
- Fixing a bug whose Mechanism answer names a class that can recur.
- Adding or changing a check: `scripts/check_*.py`, `tests/quality/`,
  `tests/architecture/`, `clients/tools/`, `tach.toml`, `pyproject.toml`
  (`[tool.ruff.lint]`), `clients/biome.json`, or `.repoctl/checks.toml`.

## Steps

1. **Climb the ladder; stop at the first rung that holds the rule.** Strongest first:
   1. Types and schemas. Pydantic models reject unknown keys
      (`tests/quality/test_models_reject_unknown_keys.py` holds every product
      model to it); closed sets are `StrEnum` or `Literal`; a cross-process
      contract has one authoritative definition with generated downstream types
      (`scripts/check_contract_sot.py`, and the `tui_quality` group regenerates
      the protocol bindings and fails on a diff).
   2. Lint rules. Ruff in `pyproject.toml`, including banned imports through
      `TID251`; Grit plugins in `clients/tools/lint-rules/`, loaded by
      `clients/biome.json`; Biome's own rules.
   3. Graph checks. Python module edges in `tach.toml` (`scripts/check_tach.py`);
      the TypeScript package graph in `clients/tools/scripts/check_package_graph.mjs`.
   4. Architecture and quality tests. `tests/architecture/` for the structure
      of product code, `tests/quality/` for repository policy and ratchets.
   5. CI scripts. `scripts/check_*.py`, run from `.repoctl/checks.toml`.
   6. Written guidance, only when every rung above is untenable (step 2).
2. **Call enforcement untenable only for one of three reasons:** the rule needs
   human judgment; a check would breed false positives and so suppressions; or
   the check would cost more CI time, or be flakier, than the bugs it prevents.
   Then write the rule with `Unenforced: <reason>`, so readers and the next
   audit see that nothing guards it. `scripts/check_skills.py` fails a rule
   item in a codebase skill that has neither a check nor the marker.
3. **Wire the check where every relevant change runs it.** A Python policy
   script joins the `python_quality` group in `.repoctl/checks.toml`; a pytest
   guard in `tests/quality/` runs in `python_guards`; a TypeScript check joins
   `tui_quality`. Paths that `.repoctl/components.toml` ignores (such as
   `.agents/`) select no job, so a check over them goes in the always-on
   `doc-checks` job of `.github/workflows/test.yml`, stdlib-only. Stay inside
   the CI wall-time budget that `scripts/ci_wall_time.py` enforces: a scan of
   the whole repository should take seconds.
4. **Ratchet existing violations; never weaken the rule to pass today.** Record
   each existing violation in a shrink-only allowlist that the check reads, as
   `scripts/check_purity.py` (with `scripts/purity_violations.json`),
   `scripts/check_file_length.py`, `scripts/check_test_isolation.py` and
   `scripts/check_skills.py` do: a new or grown violation fails. A per-site
   opt-out is a lint suppression, allowed only when every lint-compliant
   alternative would make the design more hacky, with the alternatives listed
   in the source rationale and a waiver ID (`scripts/check_lint_waivers.py`;
   Ruff's `RUF100` rejects a suppression that no longer suppresses anything).
   The waiver format is in
   [coding-best-practices.md](../../../docs/contributing/coding-best-practices.md),
   "Ratchets".
5. **Make every failure message say what to do.** Name the offending path
   (with its line) or key, the rule it breaks, and the fix, as
   `scripts/check_skills.py` does. Unenforced: whether a message is
   actionable needs a reader; the check's tests should at least assert that
   the offending path appears in its output.
6. **Cite the enforcement beside the rule.** Wherever the rule is written,
   name the check that holds it (a path such as `tests/quality/test_purity.py`,
   or a lint code such as `TID251`) or write `Unenforced: <reason>`. In codebase
   skills `scripts/check_skills.py` checks this; elsewhere, Unenforced: prose
   outside skills has no machine-readable rule structure to check.

## Testing

- Drive the check through its public functions over synthetic trees in
  `tmp_path`, with one violating and one compliant input per rule, so a check
  that matches nothing cannot pass; `tests/architecture/test_doc_citations.py`
  pairs its cases this way.
- Prefer property tests over the input space (generated names, paths, counts),
  as `tests/quality/test_check_skills.py` does for skill trees.
- Pass the tracked file list in instead of calling Git from the test; a
  deterministic-tier test runs no subprocess
  (`tests/quality/test_real_apis_confined.py`).
- Show that the check fails at the merge base for the bug it targets: run it
  on the pre-fix tree and paste the failure in the PR's Verification section.
  Unenforced: CI runs only the head; the reviewer checks the claim.

## Known failure modes

- A check gated on a hard-coded file list lets new files escape: `GATED_DOCS`
  in `scripts/check_doc_citations.py` names one doc, so citations in every
  other doc rot unchecked. Derive the scope from the tracked tree, as
  `scripts/check_skills.py` does for `.agents/skills/`.
- A check that strips the syntax it should inspect sees nothing there:
  `scripts/check_doc_links.py` removes inline code spans before matching links,
  so a path written in backticks is invisible to it;
  `scripts/check_doc_citations.py` exists to read exactly those spans.
- A rule described as enforced with no check behind it: the per-layer CPU
  budgets in [testing-map.md](../../../docs/contributing/testing-map.md) read
  as enforced, but nothing sums them. Unenforced: telling a stated guarantee
  from a description needs a reader; ask "which check fails?" in review.
- A check wired into a job the change never selects: `.repoctl/components.toml`
  ignores `.agents/`, so a skills-only PR skips `python_quality`;
  `scripts/check_skills.py` also runs in the always-on `doc-checks` job of
  `.github/workflows/test.yml` for that reason.
- An allowlist that fails only on growth goes slack: fixing one violation makes
  room for an unrelated new one. Fail on any change of a recorded count, as
  `scripts/check_skills.py` does.

## Sources of truth

- `.repoctl/checks.toml`: the check groups and their commands.
- `pyproject.toml` (`[tool.ruff.lint]`), `clients/biome.json`, `tach.toml`:
  the lint and graph rules, with their thresholds.
- [coding-best-practices.md](../../../docs/contributing/coding-best-practices.md):
  size limits, ratchets, and the lint-waiver format.
- [AGENTS.md](../../../AGENTS.md): the lint-suppression rule and the checks to
  run before a PR.
