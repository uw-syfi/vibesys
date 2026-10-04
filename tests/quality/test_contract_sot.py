"""Public contract tests for defining-authority resolution and the shrink-only gate."""

from __future__ import annotations

import json
import textwrap
from collections import Counter
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from scripts.check_contract_sot import main, measure, ratchet

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "legacy.types"


def repository(root: Path, consumer: str) -> None:
    files = {
        "src/legacy/types.py": "class OldModel:\n    first: int\n    second: str\n",
        "src/legacy/__init__.py": "from .types import OldModel as Exported\n",
        "src/legacy/bridge.py": "from . import Exported as Renamed\n",
        "src/consumer.py": consumer,
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content))
    manifest = {
        "schema_version": 1,
        "replacements": [
            {
                "module": OWNER,
                "symbol": "OldModel",
                "canonical": "vs_core.api.NewModel",
                "relation": "exact",
            }
        ],
    }
    path = root / "scripts/contract_replacements.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    "consumer",
    [
        "from legacy.types import OldModel as Alias",
        "from legacy import Exported",
        "from legacy.bridge import Renamed",
        "import legacy.types as lib\nx = lib.OldModel",
        "import legacy as lib\nx = lib.Exported",
        "import legacy.types\nx = legacy.types.OldModel",
        "import importlib as loader\nx = loader.import_module('legacy.types').OldModel",
        "from importlib import import_module as load\nm = load('legacy.types')\nx = m.OldModel",
        "m = __import__('legacy.types')\nx = m.OldModel",
        "import legacy.types as m\nx = getattr(m, 'OldModel')",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from legacy.types import OldModel",
    ],
)
def test_aliases_reexports_and_dynamic_imports_have_one_defining_authority(
    tmp_path: Path, consumer: str
) -> None:
    repository(tmp_path, consumer)
    scan = measure(tmp_path)
    assert scan.counts[("src/consumer.py", OWNER, "OldModel")] == 1
    assert not scan.errors


def test_relative_import_reexport_chain_is_counted_at_each_consumer(tmp_path: Path) -> None:
    repository(tmp_path, "from legacy.bridge import Renamed")
    scan = measure(tmp_path)
    assert scan.counts == Counter(
        {
            ("src/legacy/__init__.py", OWNER, "OldModel"): 1,
            ("src/legacy/bridge.py", OWNER, "OldModel"): 1,
            ("src/consumer.py", OWNER, "OldModel"): 1,
        }
    )


@pytest.mark.parametrize(
    ("consumer", "reason"),
    [
        ("from legacy.types import *", "star import"),
        ("class OldModel:\n    unrelated: bool", "copied frozen contract"),
        ("class RenamedCopy:\n    first: int\n    second: str", "copied frozen contract"),
    ],
)
def test_unbaselinable_violations(tmp_path: Path, consumer: str, reason: str) -> None:
    repository(tmp_path, consumer)
    assert any(reason in error for error in measure(tmp_path).errors)


@given(allowed=st.integers(0, 20), actual=st.integers(0, 20))
def test_occurrence_ratchet_cannot_grow(allowed: int, actual: int) -> None:
    key = ("src/consumer.py", OWNER, "OldModel")
    assert bool(ratchet(Counter({key: actual}), Counter({key: allowed}))) == (actual > allowed)


def baseline(root: Path, counts: Counter[tuple[str, str, str]]) -> None:
    records = [
        json.dumps({"path": path, "module": module, "symbol": symbol, "count": count})
        for (path, module, symbol), count in sorted(counts.items())
    ]
    (root / "scripts/contract_sot_baseline.jsonl").write_text("\n".join(records) + "\n")


def test_cli_requires_exact_remaining_uses_and_write_only_shrinks(tmp_path: Path) -> None:
    repository(tmp_path, "from legacy.types import OldModel")
    baseline(tmp_path, measure(tmp_path).counts)
    args = ["--root", str(tmp_path)]
    assert main(args) == 0
    consumer = tmp_path / "src/consumer.py"
    consumer.write_text(consumer.read_text() + "\nfrom legacy.types import OldModel as second\n")
    assert main([*args, "--write"]) == 1
    consumer.write_text("value = 1\n")
    assert main(args) == 1
    assert main([*args, "--write"]) == 0
    assert main(args) == 0


def test_cli_rejects_unrecognized_configuration_keys(tmp_path: Path) -> None:
    repository(tmp_path, "")
    baseline(tmp_path, measure(tmp_path).counts)
    path = tmp_path / "scripts/contract_sot_baseline.jsonl"
    path.write_text('{"path": "x", "module": "m", "symbol": "s", "count": true}\n')
    assert main(["--root", str(tmp_path)]) == 2
