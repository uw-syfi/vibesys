"""A stand-in for stitch (the trace analyser) that records its calls and writes what stitch writes.

``FakeStitch`` is an executable the tests point ``$VIBESYS_EVALUATOR_STITCH`` at. Each call
appends its arguments to ``calls.jsonl``; the command named in ``fail`` (``start``, ``collect``,
``ingest`` or ``render``) exits 1 with a message on stderr instead of doing its work. The files
are the ones the stitch command line promises, with placeholder contents.
"""

from __future__ import annotations

import json
import stat
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: What ``stitch render`` writes into its output directory.
RENDERED = (
    "README.txt",
    "report.txt",
    "report.json",
    "tree.txt",
    "critical-path.txt",
    "waterfall.txt",
    "graph-summary.json",
)

_SCRIPT = """#!{python}
import json, sys
from pathlib import Path
here = Path({root!r})
args = sys.argv[1:]
with (here / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\\n")
fail = (here / "fail").read_text() if (here / "fail").exists() else ""
verb = args[2] if args[0] == "capture" else args[0]
print(f"stitch: {{verb}} working", file=sys.stderr)
if verb == fail:
    print(f"stitch: {{verb}} failed on purpose", file=sys.stderr)
    sys.exit(1)
def flag(name):
    return args[args.index(name) + 1]
if verb == "start":
    deployments = [args[i + 1] for i, a in enumerate(args) if a == "--deployment"]
    print(json.dumps({{"namespace": flag("--namespace"), "collector_service": "otel-collector",
                      "deployments": deployments, "sampler_ratio": float(flag("--sampler-ratio")),
                      "started_unix_nanos": 1}}))
elif verb == "collect":
    out = Path(flag("--out"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "otel.jsonl").write_text('{{"span": 1}}\\n' * 100)
    windows = [args[i + 1] for i, a in enumerate(args) if a == "--window"]
    (out / "manifest.json").write_text(json.dumps({{"spans": "otel.jsonl", "windows": windows}}))
elif verb == "ingest":
    manifest = json.loads(Path(args[1]).read_text())
    spans = Path(args[1]).parent / manifest["spans"]
    Path(flag("--out")).write_text(json.dumps({{"spans": len(spans.read_text().splitlines())}}))
elif verb == "render":
    json.loads(Path(args[1]).read_text())
    out = Path(flag("--out"))
    out.mkdir(parents=True, exist_ok=True)
    for name in {rendered!r}:
        (out / name).write_text(name + "\\n")
"""


@dataclass
class FakeStitch:
    """The stitch binary of one test: set ``fail``, read ``calls``."""

    root: Path

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        binary = self.root / "stitch"
        binary.write_text(
            _SCRIPT.format(python=sys.executable, root=str(self.root), rendered=RENDERED)
        )
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        self.binary = binary

    def fail(self, verb: str) -> None:
        """Make the command ``verb`` (start, collect, ingest, render) fail."""
        (self.root / "fail").write_text(verb)

    @property
    def calls(self) -> list[list[str]]:
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def verbs(self) -> list[str]:
        return [call[2] if call[0] == "capture" else call[0] for call in self.calls]

    def call(self, verb: str) -> list[str]:
        return next(
            call for call in self.calls if (call[2] if call[0] == "capture" else call[0]) == verb
        )
