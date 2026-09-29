"""Client helpers shared by the home server tests."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tests.support import run_test_command

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from entrypoints.web_home.context import HomeConfig


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


@dataclass
class Home:
    config: HomeConfig
    workspace: Path
    default_headers: dict[str, str] = field(default_factory=dict)

    def send(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Reply:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # noqa: S310  # lint-waiver: LW-101304 [S310]; connect only to the loopback origin of the home server under test
            self.config.origin + path,
            data=data,
            method=method,
            headers=dict(self.default_headers if headers is None else headers),
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # lint-waiver: LW-101305 [S310]; connect only to the loopback request built above
                return Reply(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as error:
            return Reply(error.code, error.read(), dict(error.headers))

    def get(self, path: str) -> Reply:
        return self.send("GET", path)

    def post(self, path: str, body: object = None) -> Reply:
        return self.send("POST", path, {} if body is None else body)

    def put(self, path: str, body: object) -> Reply:
        return self.send("PUT", path, body)

    def delete(self, path: str) -> Reply:
        return self.send("DELETE", path)


MANIFEST = """version = 1

[agent]
domain = "generic"

[accuracy]
command = ["python", "check.py"]

[benchmark]
command = ["python", "bench.py"]

[benchmark.result]
json_argument = "--json"
metric = "throughput"
"""


def make_project(root: Path, *, tasks: tuple[str, ...] = ("bench",), commit: bool = True) -> Path:
    """Create a git work tree with repository-native tasks, committed unless told not to."""
    root.mkdir(parents=True, exist_ok=True)
    run_test_command(["git", "init", "-q"], cwd=root, check=True)
    run_test_command(["git", "config", "user.name", "Test"], cwd=root, check=True)
    run_test_command(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    run_test_command(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    for name in tasks:
        task = root / ".vibesys" / "tasks" / name
        task.mkdir(parents=True)
        (task / "OBJECTIVE.md").write_text(f"Make {name} faster.\n")
        (task / "vibesys.input.toml").write_text(MANIFEST)
    (root / "README.md").write_text("project\n")
    if commit:
        run_test_command(["git", "add", "-A"], cwd=root, check=True)
        run_test_command(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root.resolve()


def project_key(home: Home, root: Path) -> str:
    """Validate *root* through the API and return its project id."""
    reply = home.post("/api/projects/validate", {"path": str(root)})
    assert reply.status == 200, reply.body
    return reply.json()["project"]["id"]
