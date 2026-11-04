# Prompt goldens

A golden is a checked-in copy of rendered agent-facing text that a test compares
against the live render. It shows a reviewer exactly what an agent will read, so
the golden diff is the prompt diff. Open this file when a prompt test fails with
"golden changed" or "snapshot changed", or before changing a template that a
golden covers.

## What is pinned

Find the covering test by searching the tests for the template's role or
fragment name. Goldens exist for:

- Issue-queue role and system prompts, and its board artifacts.
- Multi and single strategy role system prompts (no regenerate switch: edit
  the fixture by hand).
- Every domain pack role, stored as a SHA-256 per domain, role, case and
  execution, so its diff shows which keys changed and not how. Render the
  changed key through the domain-section renderer to read the text.
- Run-environment notes, hypothesis notices, profile-focus ledger text,
  operator constraints folded into the objective, and interactive chat prompts.

Byte-exact goldens write the fixture instead of comparing when
`UPDATE_PROMPT_SNAPSHOTS=1` is set.

## Updating a golden

1. Make the template change and run the covering test without the variable.
   Read the failure diff: it is the change every agent will see.
2. If every changed line is intended, rerun the same test with the variable set.
3. Run it once more without the variable; it must pass.
4. Read the fixture diff in `git diff` before committing, and quote the
   meaningful part of it in the PR's Verification section.

## Fixtures that no test reads

The single and multi strategies once had prompt, progress board and event
snapshots that no test reads any more, since those strategies moved to the
vs-core lifecycle. Editing such files changes no check, and a passing suite
says nothing about them. Do not regenerate them to "fix" a prompt change; before
editing a fixture, confirm a test reads it.
