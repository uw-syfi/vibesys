# Issue Authoring

Use this guide for issues created by people, agents, scripts, or integrations.
Each form in `.github/ISSUE_TEMPLATE/` defines its sections, required fields,
and allowed values. Issues created outside the web form render each form label
as a Markdown heading, in form order, and omit an optional section only when it
has nothing useful.

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
| Roadmap | A multi-outcome direction lasting weeks or more | `05-roadmap.yml` | `type/roadmap` |

## Roadmaps and Sub-issues

- **Three levels.** A roadmap is a direction lasting weeks or more, broken into
  several outcomes. A sub-issue is one independently closable outcome, usually
  one to a few PRs. A PR is one change. Small work (a fix, a cleanup, a
  single-PR change) needs no issue.
- **Find a home first.** Before proposing a roadmap, look for an existing one
  the work fits and add a sub-issue there.
- **Approval.** A new roadmap is created only after a maintainer approves its
  plan. Agents may draft it, create it once approved, and maintain it. Agents
  may add sub-issues under an already approved roadmap.
- **Just in time.** A roadmap needs intent, end state, and scope to be
  approved, not a full task list. Create a sub-issue when its work is about to
  start. Work may start without one; when a change grows to several PRs, create
  the sub-issue then and link the PRs.
- **Progress** is the roadmap's native sub-issue count of known outcomes, which
  grows as work is discovered; the End state defines done. New work becomes a new
  sub-issue rather than growing an existing one. PRs close sub-issues
  (`Closes #N`), never the roadmap.
- **Changes.** Update the roadmap body when the plan changes, with a dated
  one-line note of what changed and why. Post a short progress comment at
  milestones (a sub-issue closed, a plan change, a blocker), not on a timer.
- **Closing.** Close the roadmap when its last sub-issue closes, with a comment
  summarizing the outcome and where follow-ups went. Parked work closes as not
  planned with the reason, or stays open with Status `Blocked` if it will
  resume.

## Titles

Use a specific, outcome-oriented title. Labels already express the issue type,
so avoid prefixes such as `[Bug]` or `[Feature]`.

Good examples:

- `Skip redundant project materialization when inputs are unchanged`
- `CLI: report missing backend dependencies in vibesys doctor`
- `Queue: MPMC bounded retryable-BUSY FIFO scenario`
- `Simulator: validate latency estimates against CUDA traces`

Avoid vague titles such as `Improve performance`, `Fix CLI`, or `Simulator
work`.

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
