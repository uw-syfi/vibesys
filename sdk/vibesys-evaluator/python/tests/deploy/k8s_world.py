"""A small app, and a stand-in for the Go core (`vibesys-evaluator`) that records its calls.

The wrapper's tests do not need a cluster or the Go binary: `FakeCore` is an executable the
tests point `$VIBESYS_EVALUATOR_BIN` at. It appends each call (verb and request) to a log and
answers from the replies the test gave; the logic behind the verbs is tested in Go (package
`k8s`, on its in-memory cluster).
"""

from __future__ import annotations

import json
import stat
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibesys_evaluator.microservices import get, k8s

if TYPE_CHECKING:
    from pathlib import Path


def make_app(**overrides: object) -> k8s.App[Any]:
    fields: dict[str, Any] = {
        "name": "shop",
        "manifest": "k8s/manifest.yaml",
        "images": {"APP_IMAGE": "app"},
        "gateway": "http://frontend:5000",
        "ready": [get("/")],
        "data_tier": "tier=data",
        "seed": lambda _data: None,
        "restartable": ["frontend", "backend"],
        "caches": {"cache": "memcached"},
    }
    fields.update(overrides)
    return k8s.App(**fields)


TARGET: dict[str, Any] = {
    "namespace": "hk8s-shop-t0",
    "uid": "uid-1",
    "task_image": "reg/vse/shop/task:1",
}

_SCRIPT = """#!{python}
import json, sys
from pathlib import Path
here = Path({root!r})
verb = sys.argv[2]
request = json.load(sys.stdin)
with (here / "calls.jsonl").open("a") as log:
    log.write(json.dumps({{"verb": verb, "request": request}}) + "\\n")
replies = json.loads((here / "replies.json").read_text())
reply = replies.get(verb, {{}})
if isinstance(reply, list):
    reply = reply.pop(0) if len(reply) > 1 else reply[0]
for name, text in (reply.pop("__outputs__", None) or {{}}).items():
    out = Path(request["outputs_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(text)
code = 1 if "error_kind" in reply else 0
print(json.dumps(reply))
sys.exit(code)
"""


@dataclass
class FakeCore:
    """The core binary of one test: set `replies[verb]`, read `calls`."""

    root: Path
    replies: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        binary = self.root / "vibesys-evaluator"
        binary.write_text(_SCRIPT.format(python=sys.executable, root=str(self.root)))
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        self.binary = binary
        self.replies.update(
            {
                "deploy": {"target": dict(TARGET)},
                "attach": {"target": dict(TARGET)},
                "run-pod": {"exit_code": 0, "log": "", "seconds": 1.0},
            }
        )
        self.save()

    def save(self) -> None:
        (self.root / "replies.json").write_text(json.dumps(self.replies))

    @property
    def calls(self) -> list[dict[str, Any]]:
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def verbs(self) -> list[str]:
        return [call["verb"] for call in self.calls]

    def request(self, verb: str) -> dict[str, Any]:
        return next(call["request"] for call in self.calls if call["verb"] == verb)
