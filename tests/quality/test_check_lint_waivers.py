"""Tests for source-annotated Ruff waiver validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.check_lint_waivers import audit, scan_source_file

if TYPE_CHECKING:
    from pathlib import Path


def write_source(root: Path, source: str) -> Path:
    path = root / "src" / "sample.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_accepts_a_suppression_with_a_colocated_reason(tmp_path: Path) -> None:
    path = write_source(
        tmp_path,
        "value = 1  # noqa: ANN201  # LW-000001; external API requires this shape\n",
    )
    assert audit(tmp_path, [path]) == []


def test_accepts_a_multiline_reason(tmp_path: Path) -> None:
    path = write_source(
        tmp_path,
        "value = 1  # noqa: ANN201  # LW-000001; external API requires this shape\n"
        "# > callers depend on the serialized field name\n",
    )
    assert audit(tmp_path, [path]) == []


def test_accepts_a_detached_reason_with_explicit_rules(tmp_path: Path) -> None:
    path = write_source(
        tmp_path,
        "# lint-waiver: LW-000001 [ANN201]; external API requires this shape\n"
        "value = 1  # noqa: ANN201\n",
    )
    assert audit(tmp_path, [path]) == []


def test_ignores_noqa_text_inside_a_string(tmp_path: Path) -> None:
    path = write_source(tmp_path, 'message = "# noqa: ANN201"\n')

    waivers, failures = scan_source_file(path, tmp_path)

    assert waivers == []
    assert failures == []


def test_requires_an_explicit_reason_and_id(tmp_path: Path) -> None:
    path = write_source(tmp_path, "value = 1  # noqa: ANN201\n")

    waivers, failures = scan_source_file(path, tmp_path)

    assert waivers == []
    assert failures == [
        "src/sample.py: source waiver comments do not match its noqa directive counts"
    ]


def test_source_rule_annotation_must_match_the_directive(tmp_path: Path) -> None:
    path = write_source(
        tmp_path,
        "# lint-waiver: LW-000001 [ANN001]; external API requires this shape\n"
        "value = 1  # noqa: ANN201\n",
    )

    failures = audit(tmp_path, [path])

    assert failures == [
        "src/sample.py: source waiver comments do not match its noqa directive counts"
    ]


def test_rejects_a_waiver_comment_that_is_not_attached_to_python_syntax(tmp_path: Path) -> None:
    path = write_source(
        tmp_path,
        "# noqa: ANN201  # LW-000001; external API requires this shape\n",
    )

    waivers, failures = scan_source_file(path, tmp_path)

    assert waivers == []
    assert failures == ["src/sample.py:1: waiver comment is not attached to Python syntax"]


def test_checks_each_rule_on_a_multi_rule_directive(tmp_path: Path) -> None:
    path = write_source(
        tmp_path,
        "value = 1  # noqa: ANN201, ANN001  # LW-000001; pytest supplies these values\n",
    )
    assert audit(tmp_path, [path]) == []


def test_requires_waiver_ids_to_be_unique_across_files(tmp_path: Path) -> None:
    first = write_source(
        tmp_path,
        "value = 1  # noqa: ANN201  # LW-000001; external API requires this shape\n",
    )
    second = tmp_path / "src" / "other.py"
    second.write_text(
        "value = 2  # noqa: ANN201  # LW-000001; external API requires this shape\n",
        encoding="utf-8",
    )

    assert audit(tmp_path, [first, second]) == [
        "src/other.py:1: LW-000001 is also used at src/sample.py:1"
    ]
