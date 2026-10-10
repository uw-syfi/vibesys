---
name: create-issue
description: Investigate, draft, and file well-scoped VibeSys GitHub issues using the repository issue forms and project conventions. Use when a user asks to create, open, or file a bug report, engineering change, expansion scenario or harness, research experiment, or roadmap issue, or asks to turn findings, TODOs, or proposed work into GitHub issues.
---

# Create Issue

## Overview

Create issues only after confirming the work is not already tracked or
implemented. Use the human forms' schemas and leave prioritization to
maintainers.

## Workflow

1. Resolve the repository and requested write scope. If the user asks only for
   a draft, do not create or modify GitHub state.
2. Read `docs/contributing/issue-authoring.md` completely.
3. Select exactly one matching form and read it completely:
   - `.github/ISSUE_TEMPLATE/01-bug.yml`
   - `.github/ISSUE_TEMPLATE/02-engineering-change.yml`
   - `.github/ISSUE_TEMPLATE/03-expansion-work.yml`
   - `.github/ISSUE_TEMPLATE/04-experiment.yml`
   - `.github/ISSUE_TEMPLATE/05-roadmap.yml`

   If the work needs several PRs, follow "Roadmaps and Sub-issues" in the doc:
   file a sub-issue under an existing roadmap; propose a new roadmap to the
   maintainer for approval instead of creating one unasked.
4. Search the codebase and both open and closed issues. Check related pull
   requests when they may show that the behavior is already implemented.
5. If the request is a duplicate or already implemented, stop before creating
   an issue. Report the supporting issue, pull request, and code evidence.
6. Draft an outcome-oriented title and a body with the form's rendered section
   headings, in form order. Keep acceptance criteria observable and testable.
7. Create the issue through the connected GitHub tooling. Apply the form's
   labels and redact credentials, tokens, private paths, and sensitive logs.
8. Add the native parent/sub-issue relationship when a parent is known. A body
   reference alone is not a substitute for the native relationship.
9. Verify membership in organization project `uw-syfi/1`. If auto-add did not
   add the issue and project write access is available, add it explicitly.
10. Set `Workstream` only when the mapping is unambiguous. Do not set Priority,
    Effort, assignee, Target date, or milestone unless the user explicitly asks
    or an established maintainer decision already exists.
11. Re-read the created issue and verify its title, body, labels, project
    membership, and parent relationship. Return its URL and any metadata that
    remains for triage.

Create sub-issues for the work about to start, each with a native parent link
to its roadmap; add later ones as they come up.

## Authoring Rules

Title and body rules are in `docs/contributing/issue-authoring.md`. Also:

- Lead with the problem, evidence, or research question rather than a proposed
  implementation.
- State non-goals for unbounded work; use checkboxes for acceptance criteria.
- Expansion work: specify observable semantics and correctness checks, not an
  optimization strategy.
- Experiments: require a decision, baseline, metric, protocol, stopping
  condition, and retained artifacts.

## GitHub Boundaries

Prefer connected GitHub tooling for issue search and creation. Use local `gh`
only for project or hierarchy operations the connector cannot perform. Resolve
field and option IDs dynamically; never hard-code project item IDs in reusable
commands.

Do not claim that duplicate searches, code inspection, reproduction, or
verification occurred unless they actually did.
