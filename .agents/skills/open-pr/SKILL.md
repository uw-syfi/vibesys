---
name: open-pr
description: Prepare and open VibeSys pull requests from local repo changes. Use when the user asks to create, open, publish, submit, or draft a PR for this repository, including tasks that need branch hygiene, targeted validation, PR intent reflection, PR template completion, commit/push, or GitHub pull request creation.
---

# Open PR

## Overview

Open a pull request for VibeSys changes without losing user work. Keep the scope narrow, recover the intended reason for the change, use the repository PR template, and make verification explicit.

## Workflow

1. Inspect state:
   - Run `git status --short --branch`.
   - Identify the current branch and upstream with `git branch --show-current` and `git remote -v`.
   - Review unstaged, staged, and untracked files before editing, staging, or committing.
   - Preserve unrelated user changes. Stage only files that belong to the requested PR.

2. Create or confirm a branch:
   - If already on an appropriate feature branch, stay on it.
   - If on `main`, `master`, or a generic branch, create a focused branch with the default Codex prefix: `vic/<short-topic>`.
   - Do not overwrite or reset a branch unless the user explicitly asks.

3. Understand the diff:
   - Use `git diff --stat`, `git diff`, and `git diff --staged` as needed.
   - Check whether generated-looking artifacts changed and verify they are intentional.
   - For prompt or skill changes, read the affected prompt/skill content as user-visible behavior.

4. Reflect on intent:
   - Gather PR motivation from the existing conversation, user request, issue links, commit history, changed files, tests, and any docs touched by the diff.
   - Treat intent as the most important part of the PR description. The `Problem` section should explain why the change exists, not merely restate which files changed.
   - Separate known intent from inference. If the reason, target user, review concern, issue linkage, rollout risk, or correctness contract is uncertain, ask the user for clarification before writing or opening the PR.
   - Ask concise questions for anything material that cannot be recovered from context unless the user explicitly asks not to be contacted.
   - Do not invent motivation to make the PR body sound complete. Use "not applicable" or a clear limitation only when the uncertainty is minor and does not change reviewer understanding.

5. Run the smallest relevant checks:
   - Prefer narrow tests first, then broaden only when the change crosses boundaries.
   - Common checks:

```bash
./scripts/format.sh
./scripts/check_format.sh
./scripts/check_lint.sh
uv run pytest path/to/test.py
uv run pytest -k keyword
uv run pytest
```

6. Commit intentionally:
   - Stage only the PR's files.
   - Re-run `git diff --staged` before committing.
   - Use a concise imperative commit subject.
   - If checks could not run, keep the commit message normal and explain the gap in the PR body.

7. Push:
   - Push the current branch to the default remote, normally `origin`.
   - Set upstream on first push: `git push -u origin <branch>`.

8. Open the PR:
   - Prefer the GitHub app/tooling when available; use `gh pr create` only as a fallback.
   - Default to a draft PR unless the user explicitly asks for a ready PR.
   - Target the repository's default base branch unless the user specifies another base.
   - Use `.github/pull_request_template.md` and fill every section.

## PR Body

Use this repository's template headings exactly:

- `Problem`: Lead with intent. Explain the maintainer or user pain, why the change is needed, what context led to it, and any issue links. This is the highest-priority section of the PR body.
- `Solution`: Describe the high-level design, important boundaries, tradeoffs, and what reviewers should inspect.
- `Verification`: Summarize automated tests, manual checks, benchmarks, or why a check was not run.
- `Correctness properties`: List invariants, contracts, expected behaviors, and compatibility constraints preserved or introduced.
- `Testing`: List exact commands/workflows and their results.

Keep the title concrete and behavior-oriented. Avoid generic titles such as "Update files" or "Fix tests."

## VibeSys Review Notes

- Mention changes to external contracts: manifests, metadata, feature flags, CLI flags, evaluator interfaces, model-serving example bundle shape, prompt output, or skill routing.
- For prompt changes, include the snapshot diff or state that snapshots were not applicable.
- For config, metadata, feature flags, and persisted state, verify both valid input and failure cases when touched.
- For `resources/skills/serving-systems/`, confirm the subtree authoring guide was followed.
- Do not include unrelated refactors, broad cleanup, or reverted user work in the PR.

## Handoff

End with the PR URL, draft/ready status, branch name, commit hash, and verification run. If any check was skipped or failed, state that plainly with the reason.
