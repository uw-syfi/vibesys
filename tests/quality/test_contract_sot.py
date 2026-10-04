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

from vs_project.api import run_git

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "legacy.types"


def repository(root: Path, consumer: str) -> None:
    files = {
        "src/vs_core/api.py": "__all__ = ['NewModel']\nclass NewModel: pass\n",
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
    (root / "scripts/contract_sot_baseline.jsonl").write_text(
        "\n".join(records) + ("\n" if records else "")
    )


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


@pytest.mark.parametrize(
    "consumer",
    [
        "import legacy.types as lib\nx = lib.OldModel\nimport typing as lib",
        "import legacy.types as lib\nimport typing as lib\nx = lib.OldModel",
        "import legacy.types as lib\nother = lib\nx = other.OldModel",
        "import importlib as loader\nload = loader.import_module\nm = load('legacy.types')\nx = m.OldModel",
        "from importlib import import_module as load\nname = 'legacy.types'\nm = load(name)\nx = m.OldModel",
        "from importlib import import_module as load\nm = load('legacy.' + 'types')\nx = m.OldModel",
    ],
)
def test_shadowed_and_assigned_aliases_preserve_legacy_occurrences(
    tmp_path: Path, consumer: str
) -> None:
    repository(tmp_path, consumer)
    scan = measure(tmp_path)
    assert scan.counts[("src/consumer.py", OWNER, "OldModel")] == 1
    assert scan.errors == ()


@pytest.mark.parametrize(
    "consumer",
    [
        "from legacy.types import OldModel as Imported\nclass Alternate(Imported): pass",
        "import legacy.types as lib\nclass Alternate(lib.OldModel): pass",
        "from legacy import Exported\nclass Alternate(Exported): pass",
    ],
)
def test_renamed_subclasses_cannot_create_an_alternate_contract_authority(
    tmp_path: Path, consumer: str
) -> None:
    repository(tmp_path, consumer)
    assert any("copied frozen contract Alternate" in error for error in measure(tmp_path).errors)


@pytest.mark.parametrize(
    "consumer",
    [
        "import importlib\nm = importlib.import_module(f'legacy.{name}')\nx = m.OldModel",
        "import legacy.types as old\nimport importlib\nm = importlib.import_module(module_name)",
        "import legacy.types as old\nimport importlib\nmodule_name = user_input()\nm = importlib.import_module(module_name)",
        "import legacy.types as old\nimport importlib\nname = 'safe.module'\nname = user_input()\nm = importlib.import_module(name)",
        "import legacy.types as old\nimport importlib\nname = 'safe.module'\ndef load(name):\n    return importlib.import_module(name)",
    ],
)
def test_unresolved_imports_with_legacy_scope_are_rejected(tmp_path: Path, consumer: str) -> None:
    repository(tmp_path, consumer)
    assert any("computed legacy import" in error for error in measure(tmp_path).errors)


def test_unrelated_computed_imports_remain_outside_the_authority_gate(tmp_path: Path) -> None:
    repository(tmp_path, "import importlib\nplugin = importlib.import_module(plugin_name)")
    assert measure(tmp_path).errors == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", "1"),
        ("replacements", True),
        ("unexpected", None),
    ],
)
def test_manifest_metadata_is_strict(tmp_path: Path, field: str, value: object) -> None:
    repository(tmp_path, "")
    baseline(tmp_path, measure(tmp_path).counts)
    path = tmp_path / "scripts/contract_replacements.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    assert main(["--root", str(tmp_path)]) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", True),
        ("path", ""),
        ("module", 1),
        ("symbol", True),
        ("count", 1.0),
        ("unknown", "value"),
    ],
)
def test_baseline_metadata_rejects_unknown_and_coerced_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    repository(tmp_path, "")
    record = {"path": "src/x.py", "module": OWNER, "symbol": "OldModel", "count": 1}
    record[field] = value
    (tmp_path / "scripts/contract_sot_baseline.jsonl").write_text(json.dumps(record) + "\n")
    assert main(["--root", str(tmp_path)]) == 2


def test_canonical_replacement_must_exist_in_the_published_api(tmp_path: Path) -> None:
    repository(tmp_path, "")
    (tmp_path / "src/vs_core/api.py").write_text("__all__ = ['Missing']\n")
    assert any("not a published" in error for error in measure(tmp_path).errors)
    (tmp_path / "src/vs_core/api.py").write_text("class NewModel: pass\n__all__ = []\n")
    assert any("not a published" in error for error in measure(tmp_path).errors)


@pytest.mark.parametrize(
    "factory",
    [
        "from pydantic import create_model\nAlternate = create_model('Alternate', __base__=OldModel)",
        "Alternate = type('Alternate', (OldModel,), {})",
    ],
)
def test_model_factories_cannot_rename_a_frozen_authority(tmp_path: Path, factory: str) -> None:
    repository(tmp_path, "from legacy.types import OldModel\n" + factory)
    assert any("model factory" in error for error in measure(tmp_path).errors)


def committed_fixture(root: Path) -> None:
    repository(root, "from legacy.types import OldModel")
    baseline(root, measure(root).counts)
    run_git(["init", "--initial-branch=main"], cwd=root).check_returncode()
    run_git(["config", "user.name", "Contract fixture"], cwd=root).check_returncode()
    run_git(["config", "user.email", "contract@example.invalid"], cwd=root).check_returncode()
    run_git(["add", "."], cwd=root).check_returncode()
    run_git(
        [
            "commit",
            "-m",
            "Contract fixture\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
        ],
        cwd=root,
    ).check_returncode()
    run_git(["checkout", "-b", "feature"], cwd=root).check_returncode()


def test_merge_base_prevents_editing_the_allowance_to_accept_growth(tmp_path: Path) -> None:
    committed_fixture(tmp_path)
    path = tmp_path / "src/consumer.py"
    path.write_text(path.read_text() + "\nfrom legacy.types import OldModel as another\n")
    baseline(tmp_path, measure(tmp_path).counts)
    assert main(["--root", str(tmp_path)]) == 0
    assert main(["--root", str(tmp_path), "--base-ref", "main"]) == 1


def test_merge_base_manifest_cannot_remove_an_authority_to_hide_its_consumers(
    tmp_path: Path,
) -> None:
    committed_fixture(tmp_path)
    manifest = tmp_path / "scripts/contract_replacements.json"
    manifest.write_text('{"schema_version": 1, "replacements": []}')
    baseline(tmp_path, measure(tmp_path).counts)
    assert main(["--root", str(tmp_path)]) == 0
    assert main(["--root", str(tmp_path), "--base-ref", "main"]) == 1


@pytest.mark.parametrize(
    "consumer",
    [
        "import importlib\nm = importlib.import_module('.types', package='legacy')\nx = m.OldModel",
        "import builtins as b\nm = b.__import__('legacy.types')\nx = m.types.OldModel",
        "import builtins as b\nimport legacy.types as m\nx = b.getattr(m, 'OldModel')",
    ],
)
def test_relative_and_builtin_dynamic_access_has_the_same_authority(
    tmp_path: Path, consumer: str
) -> None:
    repository(tmp_path, consumer)
    scan = measure(tmp_path)
    assert scan.counts[("src/consumer.py", OWNER, "OldModel")] == 1
    assert scan.errors == ()


def test_alias_resolution_cycles_fail_boundedly_without_hiding_a_legacy_scope(
    tmp_path: Path,
) -> None:
    repository(tmp_path, "import legacy.types as mod\nmod = mod.child\nx = mod.OldModel")
    assert any("bounded resolver" in error for error in measure(tmp_path).errors)
