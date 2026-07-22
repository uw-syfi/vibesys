# Issue Authoring

Use this guide for issues created by people, agents, scripts, or integrations.
The forms in `.github/ISSUE_TEMPLATE/` are the authoritative schemas.

## Core Rule

One issue describes one independently closable outcome. It should make the
problem and completion condition clear without requiring the author to choose
maintainer-owned scheduling metadata.

## Before Filing

1. Search open and closed issues for duplicates and superseded work.
2. Inspect the relevant code and merged pull requests to confirm the behavior
   is not already implemented.
3. Select exactly one issue kind from the routing table below.
4. Redact credentials, tokens, private paths, and sensitive log content.

If existing code or an issue already resolves the request, update or reference
that item instead of opening a duplicate.

## Choose an Issue Kind

| Kind | Use for | Form | Default label |
| --- | --- | --- | --- |
| Bug report | Incorrect or unexpected behavior | `01-bug.yml` | `bug` |
| Engineering change | Features, refactors, performance, or developer experience | `02-engineering-change.yml` | `enhancement` |
| Expansion work | Scenarios, shared contracts, and evaluator harnesses | `03-expansion-work.yml` | `vibeserve-expansion` |
| Research experiment | A bounded experiment intended to answer a decision-relevant question | `04-experiment.yml` | Set during triage |

Roadmap and area-parent issues are maintainer-authored planning objects. Create
them only when explicitly requested.

## Titles

Use a specific, outcome-oriented title. Labels already express the issue type,
so avoid prefixes such as `[Bug]` or `[Feature]`.

Good examples:

- `Skip redundant workspace syncs when inputs are unchanged`
- `CLI: report missing backend dependencies in vibesys doctor`
- `Queue: MPMC bounded retryable-BUSY FIFO scenario`
- `Simulator: validate latency estimates against CUDA traces`

Avoid vague titles such as `Improve performance`, `Fix CLI`, or `Simulator
work`.

## Required Schemas

API-created issues must render the form labels below as Markdown headings.
Omit an optional section only when it has no useful content.

### Bug Report

- Observed behavior
- Expected behavior
- Reproduction
- Affected subsystem
- Environment
- Impact
- Relevant logs
- Related issues or pull requests

### Engineering Change

- Workstream
- Problem
- Desired outcome
- Acceptance criteria
- Scope and non-goals
- Constraints or design considerations
- Verification approach
- Parent, dependencies, or related work

### Expansion Scenario or Harness

- Work item type
- Parent issue
- Purpose and motivation
- Required behavior or semantic contract
- Correctness checks
- Benchmark dimensions
- Non-goals
- Research or implementation anchors

### Research Experiment

- Research question and hypothesis
- Decision this experiment informs
- Baseline
- Metrics and success criteria
- Experimental protocol
- Stopping condition
- Required artifacts
- Parent or related issue

## Metadata

- **Labels** classify durable technical properties and issue kind.
- **Workstream** identifies the broad organizational home.
- **Status** records execution state.
- **Parent/sub-issue relationships** define hierarchy and progress.
- **Priority, Effort, assignee, milestone, and Target date** are maintainer
  triage decisions. Do not ask reporters to guess them.

New issues enter `Backlog`. Move an issue to `Ready` only when its outcome and
acceptance criteria are clear, dependencies are understood, and it can be
started without another discovery pass.

Use the following Workstream defaults when the mapping is clear:

- Expansion work: `Expansion`
- Research experiment: `Research/experiments`
- Engineering change: the exact value selected in its Workstream field
- Bug report: infer from the affected subsystem, or leave unset for triage

## Agent and API Checklist

When an issue is not submitted through the web form:

1. Follow the matching form's labels and section order.
2. Create a native parent/sub-issue relationship when a parent is known.
3. Verify the issue appears in the `uw-syfi/1` VibeSys Work project.
4. Set Workstream when unambiguous; leave scheduling fields for triage.
5. Re-read the created issue and verify its title, body, labels, project, and
   parent relationship.
