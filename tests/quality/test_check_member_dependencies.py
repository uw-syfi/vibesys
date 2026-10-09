"""Properties of the library dependency-declaration check.

The check compares what a workspace member imports with what its
`pyproject.toml` declares. These tests drive its public entry points with
generated declarations and with throwaway repository trees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from scripts.check_member_dependencies import Member, Workspace, check_workspace, load_workspace

if TYPE_CHECKING:
    from pathlib import Path

# import name -> distribution, as `importlib.metadata.packages_distributions` reports it
INSTALLED = {"yaml": ["PyYAML"], "pydantic": ["pydantic"], "jinja2": ["Jinja2"], "modal": ["modal"]}
DISTRIBUTIONS = {"yaml": "pyyaml", "pydantic": "pydantic", "jinja2": "jinja2", "modal": "modal"}
ROOT_PACKAGES = frozenset({"vibesys", "server"})

third_party_imports = st.sets(st.sampled_from(sorted(INSTALLED)))


def member(
    *, imports: set[str], dependencies: set[str], sources: frozenset[str] = frozenset()
) -> Member:
    return Member(
        name="vs-example",
        directory="libs/vs-example",
        dependencies=frozenset(dependencies),
        workspace_sources=frozenset(sources),
        imports=frozenset(imports),
        packages=frozenset({"vs_example"}),
    )


def workspace(*members: Member, root: set[str] | None = None) -> Workspace:
    root_dependencies = set(DISTRIBUTIONS.values()) if root is None else root
    return Workspace(tuple(members), frozenset(root_dependencies), ROOT_PACKAGES)


@given(imports=third_party_imports)
def test_declaring_exactly_what_is_imported_passes(imports: set[str]) -> None:
    declared = {DISTRIBUTIONS[name] for name in imports}

    failures = check_workspace(
        workspace(member(imports=imports | {"vs_example"}, dependencies=declared)), INSTALLED
    )

    assert failures == []


@given(imports=third_party_imports.filter(bool), data=st.data())
def test_each_undeclared_import_is_reported_by_name(imports: set[str], data: st.DataObject) -> None:
    omitted = data.draw(st.sampled_from(sorted(imports)))
    declared = {DISTRIBUTIONS[name] for name in imports - {omitted}}

    failures = check_workspace(workspace(member(imports=imports, dependencies=declared)), INSTALLED)

    assert len(failures) == 1
    assert repr(omitted) in failures[0]


@given(imports=third_party_imports, data=st.data())
def test_each_unused_declaration_is_reported_by_name(
    imports: set[str], data: st.DataObject
) -> None:
    spare = data.draw(st.sampled_from(sorted(set(INSTALLED) - imports) or ["modal"]))
    declared = {DISTRIBUTIONS[name] for name in imports} | {DISTRIBUTIONS[spare]}

    failures = check_workspace(
        workspace(member(imports=imports - {spare}, dependencies=declared)), INSTALLED
    )

    assert [DISTRIBUTIONS[spare] in failure for failure in failures] == [True]


@given(imports=third_party_imports.filter(bool))
def test_a_dependency_missing_from_the_root_is_reported(imports: set[str]) -> None:
    declared = {DISTRIBUTIONS[name] for name in imports}

    failures = check_workspace(
        workspace(member(imports=imports, dependencies=declared), root=set()), INSTALLED
    )

    assert len(failures) == len(imports)
    assert all("root project" in failure for failure in failures)


@given(upward=st.sampled_from(sorted(ROOT_PACKAGES)))
def test_importing_a_root_package_is_reported(upward: str) -> None:
    failures = check_workspace(workspace(member(imports={upward}, dependencies=set())), INSTALLED)

    assert len(failures) == 1
    assert "root distribution" in failures[0]


def test_member_edges_need_declaration_use_and_workspace_source() -> None:
    lower = Member(
        "vs-lower", "libs/vs-lower", frozenset(), frozenset(), frozenset(), frozenset({"vs_lower"})
    )

    def upper(*, imports: set[str], dependencies: set[str], sources: set[str]) -> Member:
        return Member(
            "vs-upper",
            "libs/vs-upper",
            frozenset(dependencies),
            frozenset(sources),
            frozenset(imports),
            frozenset({"vs_upper"}),
        )

    ok = upper(imports={"vs_lower"}, dependencies={"vs-lower"}, sources={"vs-lower"})
    undeclared = upper(imports={"vs_lower"}, dependencies=set(), sources=set())
    unused = upper(imports=set(), dependencies={"vs-lower"}, sources={"vs-lower"})
    unsourced = upper(imports={"vs_lower"}, dependencies={"vs-lower"}, sources=set())

    assert check_workspace(workspace(lower, ok), INSTALLED) == []
    assert "does not declare it" in check_workspace(workspace(lower, undeclared), INSTALLED)[0]
    assert "never imports it" in check_workspace(workspace(lower, unused), INSTALLED)[0]
    assert "workspace = true" in check_workspace(workspace(lower, unsourced), INSTALLED)[0]


def test_load_workspace_reads_only_library_members_from_the_tree(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "root"\ndependencies = ["Pydantic>=2"]\n'
        '[tool.uv.workspace]\nmembers = ["sdk/*", "libs/*"]\n',
        encoding="utf-8",
    )
    package = tmp_path / "libs" / "vs-a" / "src" / "vs_a"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("import pydantic\nimport os\n", encoding="utf-8")
    (tmp_path / "libs" / "vs-a" / "pyproject.toml").write_text(
        '[project]\nname = "vs-a"\ndependencies = ["pydantic>=2"]\n', encoding="utf-8"
    )
    (tmp_path / "libs" / "not-a-member" / "src").mkdir(parents=True)
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "sdk" / "x" / "src" / "x").mkdir(parents=True)
    (tmp_path / "sdk" / "x" / "pyproject.toml").write_text(
        '[project]\nname = "x"\n', encoding="utf-8"
    )

    loaded = load_workspace(tmp_path)

    assert [item.name for item in loaded.members] == ["vs-a"]
    assert loaded.members[0].imports == {"pydantic"}
    assert loaded.root_dependencies == {"pydantic"}
    assert loaded.root_packages == {"app"}
    assert check_workspace(loaded, INSTALLED) == []


SPECIFIERS = st.sampled_from([">=1", ">=1,<2", "<2,>=1", ">=3.1", "==0.10.0", ""])


@given(member_spec=SPECIFIERS, root_spec=SPECIFIERS)
def test_a_member_constraint_must_match_the_root_constraint(
    member_spec: str, root_spec: str
) -> None:
    declared = Member(
        name="vs-example",
        directory="libs/vs-example",
        dependencies=frozenset({"pydantic"}),
        workspace_sources=frozenset(),
        imports=frozenset({"pydantic"}),
        packages=frozenset({"vs_example"}),
        specifiers={"pydantic": ",".join(sorted(member_spec.split(",")))},
    )
    root = Workspace(
        (declared,),
        frozenset({"pydantic"}),
        ROOT_PACKAGES,
        {"pydantic": ",".join(sorted(root_spec.split(",")))},
    )

    failures = check_workspace(root, INSTALLED)

    agree = sorted(member_spec.split(",")) == sorted(root_spec.split(","))
    assert (failures == []) is agree
    assert all("keep one constraint" in failure for failure in failures)
