"""Executable D203 checker falsification fixtures, through its public functions."""

from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from scripts.check_purity import (
    PURE_SCOPE,
    BaselineError,
    Violation,
    compare_baseline,
    previous_baseline,
    read_baseline,
    scan_source,
    scan_value_exports,
)

from vs_project.api import run_git


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("import pathlib as p", "banned-import"),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from vs_runtime.api import Runtime",
            "io-import",
        ),
        ("from vs_project.wiring import factory", "implementation-import"),
        ("from vs_project import wiring", "implementation-import"),
        ("from builtins import open as access\naccess('x')", "builtin"),
        ("import builtins as b\nx = b.print", "builtin"),
        ("access = open", "builtin"),
        ("async def f():\n    await result", "await"),
        ("async def f():\n    async for x in stream:\n        pass", "async-for"),
        ("async def f():\n    async with resource:\n        pass", "async-with"),
        ('backend = "modal"', "implementation-literal"),
        ("import uuid as identity", "banned-import"),
        ("import multiprocessing", "banned-import"),
        ("import tempfile", "banned-import"),
    ],
)
def test_forbidden_syntax_aliases_and_type_checking_are_detected(source: str, rule: str) -> None:
    assert rule in {site.rule for site in scan_source("strategy.py", source)}


def test_pure_value_libraries_and_ordinary_strategy_text_are_allowed() -> None:
    assert (
        scan_source(
            "strategy.py",
            'import vs_prompts.api\nimport vs_evaluator_protocol.api\nlabel = "multimodal strategy"\n',
        )
        == ()
    )


def test_syntax_errors_fail_closed() -> None:
    with pytest.raises(SyntaxError):
        scan_source("strategy.py", "def invalid(:")


@given(st.integers(min_value=0, max_value=100))
def test_line_shifts_do_not_change_ratchet_identity(blank_lines: int) -> None:
    source = "class Owner:\n    async def decide(self):\n        await request\n"
    before = scan_source("strategy.py", source)
    assert scan_source("strategy.py", "\n" * blank_lines + source) == before
    assert {site.symbol for site in before} == {"Owner.decide"}


def test_new_stale_growth_and_pure_waivers_each_fail() -> None:
    site = Violation("src/vibesys/orchestration/strategy.py", "decide", "await", "request", 1)
    one = frozenset({site})
    empty = frozenset()
    assert compare_baseline(one, empty, empty)[0].startswith("new:")
    assert compare_baseline(empty, one, one)[0].startswith("stale:")
    assert compare_baseline(one, one, empty)[0].startswith("baseline growth:")
    assert compare_baseline(one, one, one) == ()
    pure = frozenset({Violation(f"{PURE_SCOPE}/new.py", "decide", "await", "request", 1)})
    assert compare_baseline(pure, pure, pure)[0].startswith("pure waiver:")


def test_unknown_baseline_keys_and_duplicate_sites_are_rejected() -> None:
    with pytest.raises(TypeError):
        read_baseline('[{"path":"x","unknown":true}]')
    with pytest.raises(BaselineError, match="duplicate"):
        read_baseline(
            '[{"path":"x","symbol":"f","rule":"await","subject":"x","occurrence":1},{"path":"x","symbol":"f","rule":"await","subject":"x","occurrence":1}]'
        )


def test_request_value_exports_allow_values_but_reject_transitive_io(tmp_path: Path) -> None:
    requests = tmp_path / "libs" / "vs-project" / "src" / "vs_project" / "api" / "requests.py"
    requests.parent.mkdir(parents=True)
    requests.write_text("from ..values import Payload\n")
    values = requests.parent.parent / "values.py"
    values.write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import asyncio\n")
    violations = scan_value_exports(tmp_path)
    assert len(violations) == 1
    assert violations[0].path.endswith("vs_project/values.py")
    assert violations[0].rule == "banned-import"
    values.write_text("from pydantic import BaseModel\nclass Payload(BaseModel):\n    value: str\n")
    assert scan_value_exports(tmp_path) == ()
    assert scan_source("strategy.py", "from vs_project.api.requests import ArtifactPut") == ()
    assert scan_source(f"{PURE_SCOPE}/bad.py", "from vs_project.api.requests import ArtifactPut")


def test_request_export_closure_checks_executed_parent_packages(tmp_path: Path) -> None:
    api = tmp_path / "libs" / "values" / "src" / "values" / "api"
    api.mkdir(parents=True)
    (api / "requests.py").write_text("class Request: pass\n")
    (api / "__init__.py").write_text("import socket\n")
    violations = scan_value_exports(tmp_path)
    assert len(violations) == 1
    assert violations[0].path.endswith("values/api/__init__.py")
    assert violations[0].subject == "socket"


def test_pure_request_export_closure_cannot_receive_a_baseline_waiver() -> None:
    pure = frozenset(
        {
            Violation(
                "libs/values/src/values/api/requests.py", "<module>", "banned-import", "socket", 1
            )
        }
    )
    assert compare_baseline(pure, pure, pure)[0].startswith("pure waiver:")


@pytest.mark.parametrize(
    "source",
    [
        "import io\nio.open('x', 'w')",
        "import sys\nsys.stdout.write('x')",
        "from io import open as access\naccess('x')",
        "import sys as process\nstream = process.stderr\nstream.write('x')",
        "import json\njson.dump({}, sink)",
        "import hashlib\nhashlib.file_digest(source, 'sha256')",
        "import importlib\nimportlib.import_module('io').open('x')",
        "import json\ngetattr(json, 'dump')({}, sink)",
        "import json\njson.__builtins__['open']('x')",
        "from collections import made_up_export",
        "import unreviewed_library",
        "__import__('io').open('x')",
    ],
)
def test_effect_paths_and_unreviewed_imports_fail_closed(source: str) -> None:
    assert scan_source("strategy.py", source)


@given(
    st.sampled_from([("io", "open"), ("sys", "stdout.write"), ("json", "dump")]),
    st.from_regex(r"alias_[a-z]{1,12}", fullmatch=True),
)
def test_effect_path_import_aliases_cannot_evade_allowlist(
    effect: tuple[str, str], alias: str
) -> None:
    module, path = effect
    source = f"import {module} as {alias}\n{alias}.{path}('x')"
    assert scan_source("strategy.py", source)


def test_pure_import_paths_and_value_aliases_remain_allowed() -> None:
    source = (
        "from collections.abc import Callable\n"
        "import json as codec\n"
        "from hashlib import sha256 as digest\n"
        "from typing import Literal\n"
        "serialize = codec.dumps\n"
        "value = serialize({'name': 'x'}, sort_keys=True)\n"
        "checksum = digest(value.encode()).hexdigest()\n"
    )
    assert scan_source("strategy.py", source) == ()


@pytest.mark.parametrize("effect", ["import io\nio.open('x')", "import sys\nsys.stdout.write('x')"])
def test_request_exports_cannot_hide_effect_paths_in_transitive_values(
    tmp_path: Path, effect: str
) -> None:
    api = tmp_path / "libs" / "values" / "src" / "values" / "api"
    api.mkdir(parents=True)
    (api / "requests.py").write_text("from ..values import Request\n")
    (api.parent / "values.py").write_text(f"class Request: pass\n{effect}\n")
    violations = scan_value_exports(tmp_path)
    assert violations
    assert all(site.path.endswith("values/values.py") for site in violations)


@pytest.mark.parametrize("history", ["linear", "merged"])
def test_previous_baseline_fetches_only_explicit_base_for_a_shallow_checkout(
    tmp_path: Path,
    history: Literal["linear", "merged"],
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    run_git(["init", "--initial-branch=main"], cwd=origin).check_returncode()
    run_git(["config", "user.name", "Purity fixture"], cwd=origin).check_returncode()
    run_git(["config", "user.email", "purity@example.invalid"], cwd=origin).check_returncode()
    baseline = origin / "scripts" / "purity_violations.json"
    baseline.parent.mkdir()
    baseline.write_text("[]\n")
    run_git(["add", "."], cwd=origin).check_returncode()
    run_git(
        [
            "commit",
            "-m",
            "Initial baseline\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
        ],
        cwd=origin,
    ).check_returncode()
    run_git(["checkout", "-b", "feature"], cwd=origin).check_returncode()
    (origin / "feature.txt").write_text("feature\n")
    run_git(["add", "."], cwd=origin).check_returncode()
    run_git(
        ["commit", "-m", "Feature\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"],
        cwd=origin,
    ).check_returncode()
    if history == "merged":
        run_git(["checkout", "main"], cwd=origin).check_returncode()
        (origin / "main.txt").write_text("main advance\n")
        run_git(["add", "."], cwd=origin).check_returncode()
        run_git(
            [
                "commit",
                "-m",
                "Main advance\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
            ],
            cwd=origin,
        ).check_returncode()
        run_git(["checkout", "feature"], cwd=origin).check_returncode()
        run_git(
            [
                "merge",
                "main",
                "-m",
                "Merge main\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
            ],
            cwd=origin,
        ).check_returncode()
    checkout = tmp_path / "checkout"
    run_git(
        [
            "clone",
            "--depth=1",
            "--single-branch",
            "--branch=feature",
            origin.as_uri(),
            str(checkout),
        ],
        cwd=tmp_path,
    ).check_returncode()
    assert previous_baseline(checkout, "origin/main") == frozenset()
    assert (
        run_git(["rev-parse", "--is-shallow-repository"], cwd=checkout, text=True).stdout.strip()
        == "false"
    )


def test_request_exports_follow_absolute_library_local_value_imports(tmp_path: Path) -> None:
    api = tmp_path / "libs" / "values" / "src" / "values" / "api"
    api.mkdir(parents=True)
    (api / "requests.py").write_text("from values.models import Request\n")
    models = api.parent / "models.py"
    models.write_text("from pydantic import BaseModel\nclass Request(BaseModel): value: str\n")
    assert scan_value_exports(tmp_path) == ()
    models.write_text("import sys\nclass Request: pass\nsys.stdout.write('x')\n")
    assert scan_value_exports(tmp_path)


def test_request_value_attributes_are_permitted_after_closure_validation() -> None:
    assert (
        scan_source(
            "strategy.py",
            "from vs_project.api.requests import ArtifactPut\nArtifactPut.model_validate(payload)",
        )
        == ()
    )


@given(
    st.sampled_from(
        [
            "copied = json",
            "copied: object = json",
            "(copied, number) = (json, 1)",
            "(copied := json)",
        ]
    )
)
def test_imported_namespace_assignment_forms_preserve_effect_paths(binding: str) -> None:
    assert scan_source("strategy.py", f"import json\n{binding}\ncopied.dump(payload, sink)")


def test_dynamic_attribute_lookup_alias_cannot_hide_an_effect_path() -> None:
    assert scan_source(
        "strategy.py", "import json\nlookup = getattr\nlookup(json, 'dump')(payload, sink)"
    )


@pytest.mark.parametrize(
    "source",
    [
        "from typing import get_type_hints\nclass C:\n    field: \"open('x', 'w').write('x')\"\nget_type_hints(C)",
        "from pydantic import BaseModel\nclass C(BaseModel):\n    field: \"open('x', 'w').write('x')\"",
        "from pydantic import TypeAdapter\nTypeAdapter(\"__import__('io').open('x')\")",
    ],
)
def test_annotation_expression_evaluators_cannot_hide_effect_paths(source: str) -> None:
    assert scan_source("strategy.py", source)


def test_ordinary_forward_value_annotations_remain_allowed() -> None:
    assert (
        scan_source(
            "strategy.py",
            'from pydantic import BaseModel, TypeAdapter\nclass Value(BaseModel):\n    child: "Value | None"\nTypeAdapter("tuple[Value, ...]")',
        )
        == ()
    )


@given(st.integers(min_value=0, max_value=3))
def test_nested_forward_type_strings_cannot_hide_effect_paths(depth: int) -> None:
    expression = "open('x', 'w').write('x')"
    for _ in range(depth):
        expression = f"tuple[{expression!r}, ...]"
    source = f"from pydantic import BaseModel\nclass C(BaseModel):\n    field: {expression!r}"
    assert scan_source("strategy.py", source)


def test_type_adapter_keyword_and_nested_forward_type_strings_are_checked() -> None:
    source = "from pydantic import TypeAdapter\nTypeAdapter(type=tuple[\"open('x')\", ...])"
    assert scan_source("strategy.py", source)


def test_literal_type_values_and_annotation_metadata_are_not_expressions() -> None:
    source = "from typing import Annotated, Literal\nfrom pydantic import BaseModel\nclass C(BaseModel):\n    field: \"Annotated[Literal['open(x)'], 'human readable label']\""
    assert scan_source("strategy.py", source) == ()


@given(st.integers(min_value=0, max_value=3))
def test_model_subclasses_cannot_inherit_unapproved_effect_methods(depth: int) -> None:
    source = "from pydantic import BaseModel\nclass C0(BaseModel):\n    field: str\n"
    for index in range(1, depth + 1):
        source += f"class C{index}(C{index - 1}):\n    pass\n"
    source += f"Alias = C{depth}\nAlias.parse_file('file.json')\n"
    assert scan_source("strategy.py", source)


@pytest.mark.parametrize(
    "base",
    [
        "from pydantic import RootModel\nclass C(RootModel[str]): pass",
        "from vs_core.api import Value\nclass C(Value): field: str",
        "from .types.common import Value\nclass C(Value): field: str",
    ],
)
def test_value_model_bases_cannot_inherit_unapproved_effect_methods(base: str) -> None:
    assert scan_source("strategy.py", base + "\nC.parse_file('file.json')")


def test_model_subclasses_preserve_approved_and_declared_value_methods() -> None:
    source = "from pydantic import BaseModel\nclass C(BaseModel):\n    field: str\n    def normalized(self): return self.field.strip()\nC.model_validate(payload)\nC.normalized(value)"
    assert scan_source("strategy.py", source) == ()


def test_super_cannot_hide_inherited_model_io_methods() -> None:
    source = "from pydantic import BaseModel\nclass C(BaseModel):\n    field: str\n    @classmethod\n    def read(cls): return super().parse_file('file.json')\nC.read()"
    assert scan_source("strategy.py", source)


@pytest.mark.parametrize("base", ["BaseModel", "RootModel", "Value"])
def test_model_parse_raw_pickle_path_is_not_an_approved_value_method(base: str) -> None:
    imports = "from vs_core.api import Value" if base == "Value" else f"from pydantic import {base}"
    source = (
        imports
        + f"\nclass C({base}):\n    field: str\nC.parse_raw(b'payload', proto='pickle', allow_pickle=True)"
    )
    assert scan_source("strategy.py", source)


def test_model_field_names_cannot_approve_shadowed_inherited_io_methods() -> None:
    source = "from pydantic import BaseModel\nclass C(BaseModel):\n    parse_file: str\nC.parse_file('file.json')"
    assert scan_source("strategy.py", source)


def test_model_class_variables_are_still_declared_values() -> None:
    source = "from typing import ClassVar\nfrom pydantic import BaseModel\nclass C(BaseModel):\n    description: ClassVar[str] = 'value'\nC.description"
    assert scan_source("strategy.py", source) == ()


@pytest.mark.parametrize(
    "source",
    [
        "from typing import TypeAliasType\nvalue = TypeAliasType",
        "import typing as types\nvalue = types.TypeAliasType",
        "from typing import TypeAliasType as Alias\nisinstance(annotation, Alias)",
    ],
)
def test_pep695_alias_type_inspection_is_pure(source: str) -> None:
    assert scan_source(f"{PURE_SCOPE}/aliases.py", source) == ()
