"""objectives.toml is parsed at the CLI boundary: unknown keys are rejected by name.

The metric space's tolerance has one sanctioned zero, the absent ``[pareto]``
table. A misspelled key must not reproduce that zero, or an empty axis list,
silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from entrypoints.cli import _load_metric_space_toml

if TYPE_CHECKING:
    from pathlib import Path

_KEY = st.from_regex(r"[a-z][a-z_]{0,15}", fullmatch=True)


def _write(root: Path, text: str) -> None:
    (root / "objectives.toml").write_text(text, encoding="utf-8")


@given(key=_KEY.filter(lambda key: key != "relative_noise"))
@example(key="relative_nosie")
@example(key="noise")
def test_unknown_pareto_key_is_rejected_by_name(
    tmp_path_factory: pytest.TempPathFactory, key: str
) -> None:
    root = tmp_path_factory.mktemp("bundle")
    _write(
        root,
        f'[[objective]]\nname = "throughput"\ndirection = "max"\n[pareto]\n{key} = 0.05\n',
    )

    with pytest.raises(ValueError, match=key):
        _load_metric_space_toml(root)


@given(key=_KEY.filter(lambda key: key not in {"name", "direction"}))
@example(key="weight")
@example(key="unit")
def test_unknown_objective_key_is_rejected_by_name(
    tmp_path_factory: pytest.TempPathFactory, key: str
) -> None:
    root = tmp_path_factory.mktemp("bundle")
    _write(root, f'[[objective]]\nname = "latency"\ndirection = "min"\n{key} = "x"\n')

    with pytest.raises(ValueError, match=key):
        _load_metric_space_toml(root)


@given(table=_KEY.filter(lambda key: key not in {"objective", "pareto"}))
@example(table="objectives")
def test_unknown_top_level_table_is_rejected_by_name(
    tmp_path_factory: pytest.TempPathFactory, table: str
) -> None:
    root = tmp_path_factory.mktemp("bundle")
    _write(root, f'[[{table}]]\nname = "latency"\ndirection = "min"\n')

    with pytest.raises(ValueError, match=table):
        _load_metric_space_toml(root)


def test_error_names_the_file_and_the_full_key_path(tmp_path: Path) -> None:
    _write(
        tmp_path, '[[objective]]\nname = "a"\ndirection = "max"\n[pareto]\nrelative_nosie = 0.1\n'
    )

    with pytest.raises(ValueError, match=r"objectives\.toml.*pareto\.relative_nosie"):
        _load_metric_space_toml(tmp_path)

    _write(tmp_path, '[[objective]]\nname = "a"\ndirection = "max"\nweight = 2\n')

    with pytest.raises(ValueError, match=r"objective\[0\]\.weight"):
        _load_metric_space_toml(tmp_path)
