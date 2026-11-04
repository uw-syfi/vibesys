"""The identity of the rules covers everything that decides a fold, derived and not listed (#3475).

A cache of a fold is valid only under the code that made it. That code is the rules (``vs_core``),
the schema the records decode with (``vs_proto``, which ``vs_core`` imports) and the reader that
routes each record to a rule (``fold_log``), and it is found from two roots by reading imports, so a
package the rules start to import is covered the day they import it. Properties over generated source
trees hold the derivation; over this repository's own tree, an independent reading of every import
(``ast``, where the derivation scans text) holds that nothing the covered code imports is left out.
"""

from __future__ import annotations

import ast
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api.recovery import rules_digest
from vs_runtime.api.step_runner import fold_log, fold_rules, fold_sources
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, MutableMapping

type Spec = MutableMapping[str, Any]
"""A directory as nested mappings: a name to the bytes of a file, or to a directory."""

READER = fold_log.__module__
"""The reader's module, the second root of the identity (the first is the package of the rules)."""
RULES = "vs_core"


class Memory(Traversable):
    """A source directory kept in memory, as much of ``Traversable`` as the derivation reads."""

    def __init__(self, name: str, spec: Spec) -> None:
        self._name = name
        self._spec = spec

    @property
    def name(self) -> str:
        return self._name

    def iterdir(self) -> Iterator[Traversable]:
        for name, entry in sorted(self._spec.items()):
            yield Memory(name, entry) if isinstance(entry, dict) else File(name, entry)

    def is_dir(self) -> bool:
        return True

    def is_file(self) -> bool:
        return False

    def read_bytes(self) -> bytes:
        raise IsADirectoryError(self._name)

    def read_text(self, encoding: str | None = None) -> str:
        del encoding
        raise IsADirectoryError(self._name)

    def open(self, mode: str = "r", *args: object, **kwargs: object) -> IO[Any]:
        raise NotImplementedError


class File(Memory):
    """One file of a ``Memory`` tree."""

    def __init__(self, name: str, content: bytes) -> None:
        super().__init__(name, {})
        self._content = content

    def iterdir(self) -> Iterator[Traversable]:
        raise NotADirectoryError(self._name)

    def is_dir(self) -> bool:
        return False

    def is_file(self) -> bool:
        return True

    def read_bytes(self) -> bytes:
        return self._content


def tree(packages: Mapping[str, Spec]) -> Callable[[str], Traversable | None]:
    """``find`` over packages given as specs: a name that is not one is not installed."""

    def find(name: str) -> Traversable | None:
        return Memory(name, packages[name]) if name in packages else None

    return find


def importing(*modules: str) -> bytes:
    return "".join(f"from {module} import x\n" for module in modules).encode()


def world(**extra: Spec) -> dict[str, Spec]:
    """The packages of a small repository: rules that import ``vs_proto``, a reader beside a shell."""
    return {
        RULES: {
            "__init__.py": b"",
            "_rules.py": importing("vs_proto.api"),
            "testing": {"drive.py": importing("vs_runtime._shell")},
        },
        "vs_proto": {
            "__init__.py": b"",
            "api.py": importing("vs_decode.api"),
            "_gen": {"m.py": b"1"},
        },
        "vs_decode": {"__init__.py": b"", "api.py": b"2"},
        "vs_runtime": {
            "__init__.py": importing("vs_agent"),
            "_saved_run_record.py": importing(RULES + ".api", "vs_runtime._names"),
            "_names.py": b"3",
            "_shell.py": importing("vs_agent"),
            "api": {"__init__.py": b"4", "x.py": b"5"},
        },
        "vs_agent": {"__init__.py": b"6"},
        **extra,
    }


COVERED = {
    f"{RULES}/__init__.py",
    f"{RULES}/_rules.py",
    "vs_proto/__init__.py",
    "vs_proto/api.py",
    "vs_proto/_gen/m.py",
    "vs_decode/__init__.py",
    "vs_decode/api.py",
    "vs_runtime/_saved_run_record.py",
    "vs_runtime/_names.py",
}
"""What `world` is read for: the rules whole, what they import whole, the reader and what it imports alone."""


def test_the_sources_are_the_rules_the_packages_they_import_and_the_reader_with_its_own_imports() -> (
    None
):
    """Not the test support of the rules, not the shell around the reader, not what the shell imports."""
    assert set(fold_sources(tree(world()))) == COVERED


def test_a_package_the_rules_start_to_import_is_covered_without_anyone_listing_it() -> None:
    sources = fold_sources(
        tree(
            world(
                vs_new={"__init__.py": b"7", "deep": {"x.py": importing("vs_newer")}},
                vs_newer={"__init__.py": b"8"},
                **{RULES: {"__init__.py": b"", "_rules.py": importing("vs_proto.api", "vs_new")}},
            )
        )
    )
    assert {"vs_new/__init__.py", "vs_new/deep/x.py", "vs_newer/__init__.py"} <= set(sources)


def test_a_module_imported_as_a_package_is_read_as_its_init() -> None:
    packages = world(
        vs_runtime={
            "_saved_run_record.py": importing(RULES + ".api", "vs_runtime.api"),
            "api": {"__init__.py": b"4", "x.py": b"5"},
        }
    )
    assert "vs_runtime/api/__init__.py" in fold_sources(tree(packages))


@pytest.mark.parametrize("missing", [RULES, "vs_proto", "vs_decode", "vs_runtime"])
def test_a_build_that_ships_no_source_for_any_of_it_has_no_identity(missing: str) -> None:
    """Never a partial identity: it would match the cache of a build that differs in what it lacks."""
    packages = world()
    del packages[missing]
    assert fold_sources(tree(packages)) == {}
    assert rules_digest(fold_sources(tree(packages))) == ""


def test_a_package_with_no_python_files_is_no_source() -> None:
    assert fold_sources(tree(world(vs_decode={"data.bin": b"\0"}))) == {}


NAMES = st.sampled_from(sorted(COVERED))


@settings(max_examples=examples(pr=20, nightly=200), deadline=None)
@given(changed=NAMES, extra=st.binary(min_size=1, max_size=4))
def test_a_change_to_any_file_that_decides_a_fold_changes_the_identity(
    changed: str, extra: bytes
) -> None:
    packages = world()
    before = rules_digest(fold_sources(tree(packages)))
    package, _, path = changed.partition("/")
    spec = packages[package]
    for part in path.split("/")[:-1]:
        spec = spec[part]
    leaf = path.split("/")[-1]
    spec[leaf] = spec[leaf] + b"#" + extra

    assert rules_digest(fold_sources(tree(packages))) != before


@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
@given(extra=st.binary(min_size=1, max_size=4))
@pytest.mark.parametrize(
    "ignored",
    [
        ("vs_runtime", "_shell.py"),
        ("vs_agent", "__init__.py"),
        (RULES, "testing/drive.py"),
    ],
)
def test_a_change_to_a_file_that_decides_no_fold_changes_nothing(
    ignored: tuple[str, str], extra: bytes
) -> None:
    packages = world()
    before = rules_digest(fold_sources(tree(packages)))
    package, path = ignored
    spec = packages[package]
    for part in path.split("/")[:-1]:
        spec = spec[part]
    leaf = path.split("/")[-1]
    spec[leaf] = spec[leaf] + b"#" + extra

    assert rules_digest(fold_sources(tree(packages))) == before


def test_the_identity_of_this_process_is_the_digest_of_the_sources_it_derives() -> None:
    sources = fold_sources()
    assert fold_rules() == rules_digest(sources) != ""
    assert f"{RULES}/_decide.py" in sources
    assert not [path for path in sources if "/testing/" in path]
    assert {path.partition("/")[0] for path in sources} >= {RULES, "vs_proto", "vs_runtime"}
    assert [path for path in sources if path.startswith("vs_runtime/")] == [
        f"vs_runtime/{READER.partition('.')[2]}.py"
    ]


def _first_party_imports(source: bytes) -> set[str]:
    """Every first-party module a file imports at any depth, read as syntax and not as text."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {name for name in found if name.startswith("vs_")}


def test_nothing_a_covered_file_imports_is_left_out_of_the_identity() -> None:
    """An independent reading of the real tree: every import of ours lands in the covered files.

    A package the covered code imports is covered, and a module of the reader's own package is
    covered as that file, or as the init of the package it names.
    """
    sources = fold_sources()
    packages = {path.partition("/")[0] for path in sources}
    reader_package = READER.partition(".")[0]
    for path, source in sources.items():
        for module in _first_party_imports(source):
            top, _, rest = module.partition(".")
            assert top in packages, (path, module)
            if top == reader_package:
                leaf = rest.replace(".", "/") if rest else "__init__"
                assert {f"{top}/{leaf}.py", f"{top}/{leaf}/__init__.py"} & set(sources), (
                    path,
                    module,
                )


@pytest.mark.parametrize("package", ["vs_core", "vs_proto", "vs_decode"])
def test_a_package_the_reader_and_the_rules_depend_on_is_covered_in_every_file(
    package: str,
) -> None:
    """Whole, as the old identity covered ``vs_core``: every source file but test support and caches."""
    root = Path(str(resources.files(package)))
    expected = {
        f"{package}/{path.relative_to(root).as_posix()}"
        for path in root.rglob("*.py")
        if not {"testing", "__pycache__"} & set(path.relative_to(root).parts)
    }
    assert {path for path in fold_sources() if path.startswith(f"{package}/")} == expected
