# Prompt goldens

A golden is a checked-in copy of rendered agent-facing text that a test compares
against the live render. It shows a reviewer exactly what an agent will read, so
the golden diff is the prompt diff. Open this file when a prompt test fails with
"golden changed" or "snapshot changed", or before changing a template that a
golden below covers.

## Which golden covers what

| What it pins | Test | Fixture files | Regenerate |
| --- | --- | --- | --- |
| Issue-queue role prompts and system prompts, byte for byte | `tests/vibesys/orchestration/issue_queue/test_prompt_goldens.py` | `tests/vibesys/orchestration/issue_queue/fixtures/prompts/` | `UPDATE_PROMPT_SNAPSHOTS=1` |
| Issue-queue board artifacts | `tests/vibesys/orchestration/issue_queue/test_artifact_goldens.py` | `tests/vibesys/orchestration/issue_queue/fixtures/board/` | `UPDATE_PROMPT_SNAPSHOTS=1` |
| Multi and single role system prompts | `tests/vibesys/orchestration/multi/test_system_prompts.py`, `tests/vibesys/orchestration/single/test_system_prompts.py` | `tests/vibesys/orchestration/multi/fixtures/exact_prompts/`, `tests/vibesys/orchestration/single/fixtures/exact_prompts/` | none: edit the fixture by hand |
| Every domain pack role, as a SHA-256 per (domain, role, case, execution) | `tests/vibesys/orchestration/domains/test_prompt_snapshots.py` | `tests/vibesys/orchestration/domains/fixtures/prompt_sha256.json` | `UPDATE_PROMPT_SNAPSHOTS=1` |
| Run-environment notes (Modal, Docker) | `tests/vibesys/sandbox/test_environment_prompt_snapshots.py` | `tests/vibesys/sandbox/fixtures/environment_prompt_snapshots/` | `UPDATE_PROMPT_SNAPSHOTS=1` |
| Hypothesis notices (regression, exhaustion, Pareto archive) | `tests/vibesys/orchestration/hypothesis/test_notice_goldens.py` | `tests/vibesys/orchestration/hypothesis/notice_goldens/` | `UPDATE_PROMPT_SNAPSHOTS=1` |
| Profile-focus ledger text | `tests/vibesys/orchestration/profile_focus/test_ledger_goldens.py` | `tests/vibesys/orchestration/profile_focus/ledger_goldens/` | `UPDATE_PROMPT_SNAPSHOTS=1` |
| Operator constraints folded into the objective | `tests/vibesys/api/test_operator_constraints_goldens.py` | `tests/vibesys/api/fixtures/operator_constraints/` | `UPDATE_PROMPT_SNAPSHOTS=1` |
| TUI chat prompts | `tests/server/test_chat_prompt_goldens.py` | `tests/server/fixtures/chat_prompts/` | `UPDATE_PROMPT_SNAPSHOTS=1` |

Byte-exact goldens compare through
`tests/vibesys/golden/helpers.py:assert_exact_text`, which writes the fixture
instead of comparing when `UPDATE_PROMPT_SNAPSHOTS=1` is set.

## Updating a golden

1. Make the template change and run the covering test without the variable.
   Read the failure diff: it is the change every agent will see.
2. If every changed line is intended, rerun the same test with the variable set,
   for example
   `UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest tests/vibesys/orchestration/issue_queue/test_prompt_goldens.py`.
3. Run it once more without the variable; it must pass.
4. Read the fixture diff in `git diff` before committing, and quote the
   meaningful part of it in the PR's Verification section.

The domain manifest stores only digests, so its diff shows which keys changed
and not how. Render the changed key with
`src/vibesys/domains/rendering.py:render_domain_section` to read the text.

## Fixtures that no test reads

`tests/vibesys/golden/snapshots/` (prompts, progress boards and events for the
single and multi strategies) is read by no test since those strategies moved to
the vs-core lifecycle (#2032, #2044): `assert_prompt_snapshot` in
`tests/vibesys/golden/helpers.py` has no caller. Editing those files changes no
check, and a passing suite says nothing about them. Do not regenerate them to
"fix" a prompt change; the goldens in the table above are the live ones.
