# vs-github

`vs-github` is the reusable GitHub CLI boundary for VibeSys. It uses the
credentials already managed by `gh`, verifies authentication before remote
operations, and converts subprocess failures into actionable Python errors.
It is an internal import package shipped by the `vibesys` distribution, not a
separately published Python distribution.

```python
from vs_github.api import GitHubCLI

github = GitHubCLI()
github.clone_repository("owner/experiment", destination)
```

Interactive users authenticate with `gh auth login`. Automated environments
can provide the standard `GH_TOKEN` environment variable consumed by `gh`.
