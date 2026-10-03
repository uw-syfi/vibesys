# vs-issue-tracker

Reusable issue-tracking and progress-log contracts with local and GitHub
implementations for agent workflows.

This is an internal import package shipped by the `vibesys` distribution. It
is not published as a separate Python distribution.

## Responsibility

This package owns the storage-neutral `IssueTracker` and `ProgressLog`
contracts, local and GitHub implementations, issue state and history, create
policies, text formatting, backend configuration, and run-scoped sessions.
Applications provide paths, run identity, and loop policy.

## Concepts

- `IssueTracker` defines issue operations independently of storage. `IssueBoard`
  implements it with local JSON; `GitHubIssueTracker` implements it with
  GitHub Issues.
- `ProgressLog` is a separate append/read contract with local file and
  GitHub-backed implementations.
- `IssueTrackerSession` opens the selected issue and progress implementations
  together.
- `Issue`, `IssueEvent`, `IssueStatus`, and `IssueType` are typed Pydantic
  models/enums for issue state and history.
- `IssueBoard.reload()` lets multiple processes coordinate through the same
  file.
- A failed load or reload raises `IssueBoardLoadError` and preserves the last
  valid in-memory state.
- `on_change` lets applications attach derived views such as markdown mirrors
  without making rendering part of the core library.
- `CreateIssuePolicy` keeps role-specific create limits out of application
  wrappers.

GitHub issue types use `vibesys:type/*` labels. In-progress and blocked states
use `vibesys:status/*` labels, while GitHub's native open/closed state handles
open and closed issues. Versioned HTML-comment records preserve actor,
iteration, attempt counts, and event history without replacing user bodies or
comments. Per-run progress uses a separate `vibesys:progress-log` issue whose
versioned comments contain entries. Authenticate with `gh auth login` before
selecting the GitHub backend.

## Example

```python
from pathlib import Path

from vs_issue_tracker.api import IssueBoard, IssueStatus, IssueType

board = IssueBoard(Path("issues.json"))
issue = board.create(
    type=IssueType.BUG,
    title="Fix startup crash",
    description="Server exits before binding a port.",
    created_by="agent",
    iteration=1,
)

board.update_status(issue.id, IssueStatus.IN_PROGRESS, actor="agent", iteration=1)
```

## Create Policies

Use `CreateIssuePolicy` when a caller should only be allowed to create certain
issue types, or when creation should be capped per creator and iteration.

```python
from vs_issue_tracker.api import (
    CreateIssuePolicy,
    IssueBoard,
    IssueType,
    create_issue_under_policy,
)

board = IssueBoard("issues.json")
policy = CreateIssuePolicy(
    creator="judge",
    iteration=3,
    cap=1,
    allowed_types=frozenset({IssueType.BUG}),
)

issue, message = create_issue_under_policy(
    board,
    type_str="bug",
    title="Handle failed health check",
    description="The server should retry transient health check failures.",
    policy=policy,
)
```

`message` is suitable to return directly to an agent-facing tool. On success it
is `created issue #N`; on rejection it is a stable error string.

## Ownership Boundary

Keep generic behavior in this package when it can be reused without importing
VibeSys. Examples: persistence selection, issue lifecycle state, type
validation, formatting, create policies, and progress storage.

Keep application-specific behavior outside the package. Examples: prompt text,
loop scheduling, markdown report rendering, and VibeSys-specific CLI flags.

## Testing

Package-owned tests live beside the package:

```bash
uv run pytest libs/vs-issue-tracker/tests
```
