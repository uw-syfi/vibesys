# vs-github

This internal package uses credentials managed by `gh` and is shipped with
the `vibesys` distribution.

## Responsibility

This package wraps `gh` for repository operations, including authentication
checks and actionable errors. Applications choose when to perform those
operations and own their repository workflow.

## Usage

```python
from vs_github.api import GitHubCLI

github = GitHubCLI()
github.clone_repository("owner/experiment", destination)
```

Interactive users authenticate with `gh auth login`. Automated environments
can provide the standard `GH_TOKEN` environment variable consumed by `gh`.
