# vs-issue-board

Reusable JSON-backed issue board utilities for small agent workflows.

This is an internal import package shipped by the `vibesys` distribution. It
is not published as a separate Python distribution.

## Responsibility

This package owns reusable issue state and history, JSON persistence, create
policies, text formatting, and a stdio MCP server. Applications supply prompts,
rendering, agent tools, and loop orchestration around that board.

## Concepts

- `IssueBoard` stores issues in one JSON file and writes atomically with a
  temporary file plus rename.
- `Issue`, `IssueEvent`, `IssueStatus`, and `IssueType` are typed Pydantic
  models/enums for issue state and history.
- `IssueBoard.reload()` lets multiple processes coordinate through the same
  file.
- A failed load or reload raises `IssueBoardLoadError` and preserves the last
  valid in-memory state.
- `on_change` lets applications attach derived views such as markdown mirrors
  without making rendering part of the core library.
- `CreateIssuePolicy` keeps role-specific create limits out of application
  wrappers, so MCP and in-process tool paths can share the same semantics.

## Example

```python
from pathlib import Path

from vs_issue_board import IssueBoard, IssueStatus, IssueType

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
from vs_issue_board import (
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

## MCP Server

The package ships a stdio MCP server for generic issue-board access:

```bash
uv run vibesys-issue-mcp issues.json --creator agent --allowed-types bug,feature,perf
```

It can also be launched as a module:

```bash
uv run python -m vs_issue_board.mcp issues.json --read-only
```

The server registers:

- `list_issues`
- `get_issue`
- `search_issues`
- `create_issue`, unless `--read-only` is passed

Useful options:

- `--creator`: value recorded as `created_by` for new issues.
- `--iteration`: iteration number used for create-cap accounting.
- `--cap`: max issues this creator may file in the iteration.
- `--allowed-types`: comma-separated subset of `bug,feature,perf`.
- `--read-only`: expose only read/search tools.

## Ownership Boundary

Keep generic behavior in this package when it can be reused without importing
VibeSys. Examples: persistence, issue lifecycle state, type validation,
formatting, create policies, and MCP access.

Keep application-specific behavior outside the package. Examples: prompt text,
loop scheduling, markdown report rendering, agent tool adapters, and
VibeSys-specific CLI wiring.

## Testing

Package-owned tests live beside the package:

```bash
uv run pytest libs/vs-issue-board/tests
```
