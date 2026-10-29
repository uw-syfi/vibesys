"""Tests for versioned evaluator package contracts and local resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vs_runtime.api.infrastructure import (
    EvaluatorPackageError,
    EvaluatorPackageNotFoundError,
    EvaluatorPackageRequirement,
    load_evaluator_package,
    resolve_evaluator_package,
)
from vs_sandbox.api.evaluator_tools import CargoGitToolSpec, tool_token

if TYPE_CHECKING:
    from pathlib import Path


def _write_package(
    root: Path,
    *,
    name: str = "vibesys-evaluator-test",
    version: str = "1.2.3",
    extra_metadata: str = "",
    entrypoint_command: str = '"python", "${PACKAGE_ROOT}/runner.py"',
) -> Path:
    root.mkdir(parents=True)
    (root / "runner.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "vibesys.evaluator.toml").write_text(
        f'''schema_version = 1
name = "{name}"
version = "{version}"
protocol_version = 1
{extra_metadata}
[entrypoints]
test-check = [{entrypoint_command}]
''',
        encoding="utf-8",
    )
    return root


def test_load_package_validates_metadata_and_expands_entrypoint(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "packages" / "test")

    package = load_evaluator_package(root)

    assert package.name == "vibesys-evaluator-test"
    assert package.version == "1.2.3"
    assert package.digest.startswith("sha256:")
    assert package.command("test-check", "--case", "smoke") == (
        "python",
        f"{root}/runner.py",
        "--case",
        "smoke",
    )
    assert package.command(
        "test-check",
        "--project",
        "${PROJECT_ROOT}",
        project_root=tmp_path,
    )[-2:] == ("--project", str(tmp_path))


def test_python_token_is_preserved_for_environment_resolution(tmp_path: Path) -> None:
    root = _write_package(
        tmp_path / "package", entrypoint_command='"${PYTHON}", "${PACKAGE_ROOT}/runner.py"'
    )

    package = load_evaluator_package(root)

    assert package.command("test-check") == ("${PYTHON}", f"{root}/runner.py")


def test_package_rejects_embedded_python_token(tmp_path: Path) -> None:
    root = _write_package(
        tmp_path / "package",
        entrypoint_command='"prefix-${PYTHON}", "${PACKAGE_ROOT}/runner.py"',
    )

    with pytest.raises(EvaluatorPackageError, match=r"Python token.*complete argv element"):
        load_evaluator_package(root)


@pytest.mark.parametrize(
    "entrypoint_command",
    [
        '"go", "-C", "${PACKAGE_ROOT}", "run", "."',
        '"env", "GODEBUG=x", "go", "run", "."',
        '"env", "-i", "GODEBUG=x", "/usr/local/bin/go", "run", "."',
        '"command", "-p", "go", "run", "."',
        '"command", "--", "env", "GODEBUG=x", "go", "run", "."',
        '"env", "-S", "\\"go\\" run ."',
        '"sh", "-c", "exec \\"go\\" run ."',
        '"sh", "-c", "exec go \\"$1\\" .", "vibesys-go", "run"',
        '"bash", "-c", "GODEBUG=x exec /usr/local/bin/go -C . run ."',
    ],
)
def test_supported_go_run_wrappers_require_a_declared_package_module(
    tmp_path: Path,
    entrypoint_command: str,
) -> None:
    root = _write_package(
        tmp_path / "package",
        entrypoint_command=entrypoint_command,
    )

    with pytest.raises(EvaluatorPackageError, match="uses go run but declares no go_module"):
        load_evaluator_package(root)


@pytest.mark.parametrize(
    "entrypoint_command",
    [
        '"printf", "%s %s", "go", "run"',
        '"sh", "-c", "echo go run ."',
        '"command", "-v", "go", "run"',
        '"go", "-C", "run", "version"',
    ],
)
def test_unrelated_go_and_run_arguments_do_not_require_a_module(
    tmp_path: Path,
    entrypoint_command: str,
) -> None:
    root = _write_package(
        tmp_path / "package",
        entrypoint_command=entrypoint_command,
    )

    package = load_evaluator_package(root)

    assert package.metadata.go_module is None


def test_env_wrapped_go_run_accepts_a_complete_declared_module(tmp_path: Path) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata='toolchains = ["go"]\ngo_module = "module"\n',
        entrypoint_command=('"env", "GODEBUG=x", "go", "-C", "${PACKAGE_ROOT}/module", "run", "."'),
    )
    module = root / "module"
    module.mkdir()
    (module / "go.mod").write_text("module example.com/evaluator\n", encoding="utf-8")
    (module / "go.sum").write_text("", encoding="utf-8")

    package = load_evaluator_package(root)

    assert package.go_module_root == module


@pytest.mark.parametrize("excluded", [".git", "__pycache__", "target"])
def test_go_module_cannot_hide_below_a_digest_excluded_directory(
    tmp_path: Path,
    excluded: str,
) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata=f'toolchains = ["go"]\ngo_module = "{excluded}/module"\n',
    )
    module = root / excluded / "module"
    module.mkdir(parents=True)
    (module / "go.mod").write_text("module example.com/evaluator\n", encoding="utf-8")
    (module / "go.sum").write_text("", encoding="utf-8")

    with pytest.raises(
        EvaluatorPackageError,
        match=rf"go_module cannot use digest-excluded path component '{excluded}'",
    ):
        load_evaluator_package(root)


@pytest.mark.parametrize("target", ["./target/dep", "../outside-dep"])
def test_local_go_replace_target_must_be_digest_covered_package_content(
    tmp_path: Path,
    target: str,
) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata='toolchains = ["go"]\ngo_module = "."\n',
    )
    (root / "go.mod").write_text(
        "module example.com/evaluator\n\n"
        "go 1.21\n\n"
        "require example.com/dep v0.0.0\n"
        f"replace example.com/dep => {target}\n",
        encoding="utf-8",
    )
    (root / "go.sum").write_text("", encoding="utf-8")
    target_root = (root / target).resolve()
    target_root.mkdir(parents=True)
    (target_root / "go.mod").write_text("module example.com/dep\n", encoding="utf-8")

    with pytest.raises(
        EvaluatorPackageError,
        match=r"replace directive.*(digest-excluded|outside the evaluator package)",
    ):
        load_evaluator_package(root)


def test_local_go_replace_validation_recurses_through_covered_modules(tmp_path: Path) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata='toolchains = ["go"]\ngo_module = "."\n',
    )
    (root / "go.mod").write_text(
        "module example.com/evaluator\n\n"
        "go 1.21\n\n"
        "require example.com/dep v0.0.0\n"
        "replace example.com/dep => ./modules/dep\n",
        encoding="utf-8",
    )
    (root / "go.sum").write_text("", encoding="utf-8")
    dependency = root / "modules/dep"
    dependency.mkdir(parents=True)
    (dependency / "go.mod").write_text(
        "module example.com/dep\n\n"
        "go 1.21\n\n"
        "require example.com/hidden v0.0.0\n"
        "replace example.com/hidden => ../../target/hidden\n",
        encoding="utf-8",
    )
    hidden = root / "target/hidden"
    hidden.mkdir(parents=True)
    (hidden / "go.mod").write_text("module example.com/hidden\n", encoding="utf-8")

    with pytest.raises(
        EvaluatorPackageError,
        match=r"go\.mod:.*replace directive.*digest-excluded path component 'target'",
    ):
        load_evaluator_package(root)


def test_covered_local_go_replace_module_participates_in_package_digest(tmp_path: Path) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata='toolchains = ["go"]\ngo_module = "."\n',
    )
    (root / "go.mod").write_text(
        "module example.com/evaluator\n\n"
        "go 1.21\n\n"
        "require example.com/dep v0.0.0\n"
        "replace (\n"
        "    example.com/dep => ./modules/dep\n"
        ")\n",
        encoding="utf-8",
    )
    (root / "go.sum").write_text("", encoding="utf-8")
    dependency = root / "modules/dep"
    dependency.mkdir(parents=True)
    nested_go_mod = dependency / "go.mod"
    nested_go_mod.write_text("module example.com/dep\n\ngo 1.21\n", encoding="utf-8")
    original = load_evaluator_package(root).digest

    nested_go_mod.write_text("module example.com/dep\n\ngo 1.22\n", encoding="utf-8")

    assert load_evaluator_package(root).digest != original


def test_versioned_remote_go_replace_does_not_require_package_content(tmp_path: Path) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata='toolchains = ["go"]\ngo_module = "."\n',
    )
    (root / "go.mod").write_text(
        "module example.com/evaluator\n\n"
        "go 1.21\n\n"
        "replace example.com/dep v1.0.0 => example.com/fork v1.2.0\n",
        encoding="utf-8",
    )
    (root / "go.sum").write_text("", encoding="utf-8")

    package = load_evaluator_package(root)

    assert package.go_module_root == root


@pytest.mark.parametrize(
    "target",
    ["`./modules/dep`", r'"\u002e\u002e/\u0074arget/dep"'],
)
def test_opaque_local_go_replace_names_the_unsupported_directive(
    tmp_path: Path,
    target: str,
) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata='toolchains = ["go"]\ngo_module = "."\n',
    )
    (root / "go.mod").write_text(
        f"module example.com/evaluator\n\nreplace example.com/dep => {target}\n",
        encoding="utf-8",
    )
    (root / "go.sum").write_text("", encoding="utf-8")
    literal_spellings = {target.strip('`"'), target.strip('`"').replace("\\", "")}
    for spelling in literal_spellings:
        literal_target = root / spelling
        literal_target.mkdir(parents=True)
        (literal_target / "go.mod").write_text("module example.com/dep\n", encoding="utf-8")

    with pytest.raises(
        EvaluatorPackageError,
        match=r"go\.mod:3 has unsupported replace directive.*dep",
    ):
        load_evaluator_package(root)


@pytest.mark.parametrize(("missing", "error"), [("go.mod", "go.mod"), ("go.sum", "go.sum")])
def test_declared_go_module_requires_complete_module_files(
    tmp_path: Path, missing: str, error: str
) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata='toolchains = ["go"]\ngo_module = "module"\n',
        entrypoint_command='"go", "-C", "${PACKAGE_ROOT}/module", "run", "."',
    )
    module = root / "module"
    module.mkdir()
    for name in {"go.mod", "go.sum"} - {missing}:
        (module / name).write_text("module example.com/evaluator\n", encoding="utf-8")

    with pytest.raises(EvaluatorPackageError, match=rf"required {error} is missing"):
        load_evaluator_package(root)


def test_declared_go_module_is_exposed_relative_to_the_immutable_package(
    tmp_path: Path,
) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata='toolchains = ["go"]\ngo_module = "module"\n',
    )
    module = root / "module"
    module.mkdir()
    (module / "go.mod").write_text("module example.com/evaluator\n", encoding="utf-8")
    (module / "go.sum").write_text("", encoding="utf-8")

    package = load_evaluator_package(root)

    assert package.go_module_root == module


def test_digest_covers_paths_contents_and_executable_mode(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "package")
    original = load_evaluator_package(root).digest

    runner = root / "runner.py"
    runner.write_text("print('changed')\n", encoding="utf-8")
    changed_contents = load_evaluator_package(root).digest
    runner.chmod(0o755)
    changed_mode = load_evaluator_package(root).digest

    assert len({original, changed_contents, changed_mode}) == 3


def test_digest_ignores_interpreter_cache(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "package")
    original = load_evaluator_package(root).digest

    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "runner.cpython-312.pyc").write_bytes(b"generated")

    assert load_evaluator_package(root).digest == original


def test_package_rejects_symlinks(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "package")
    (root / "runner-link.py").symlink_to(root / "runner.py")

    with pytest.raises(EvaluatorPackageError, match="may not contain symlinks"):
        load_evaluator_package(root)


def test_registry_resolves_an_exact_version(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    expected = _write_package(packages / "v1", version="1.0.0")
    _write_package(packages / "v2", version="2.0.0")

    package = resolve_evaluator_package(
        packages, EvaluatorPackageRequirement(name="vibesys-evaluator-test", version="1.0.0")
    )

    assert package.root == expected
    assert package.version == "1.0.0"


def test_registry_reports_available_versions(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    _write_package(packages / "v1", version="1.0.0")

    with pytest.raises(
        EvaluatorPackageNotFoundError,
        match=r"vibesys-evaluator-test==2\.0\.0.*available packages: "
        r"vibesys-evaluator-test==1\.0\.0",
    ):
        resolve_evaluator_package(
            packages, EvaluatorPackageRequirement(name="vibesys-evaluator-test", version="2.0.0")
        )


def test_registry_rejects_duplicate_name_and_version(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    _write_package(packages / "first")
    _write_package(packages / "second")

    with pytest.raises(EvaluatorPackageError, match="duplicate evaluator package"):
        resolve_evaluator_package(
            packages, EvaluatorPackageRequirement(name="vibesys-evaluator-test", version="1.2.3")
        )


def test_metadata_rejects_unknown_fields(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "package", extra_metadata="unknown = true\n")

    with pytest.raises(EvaluatorPackageError, match="invalid evaluator package metadata"):
        load_evaluator_package(root)


def test_load_package_validates_cargo_git_tool_and_token(tmp_path: Path) -> None:
    revision = "1" * 40
    root = _write_package(
        tmp_path / "package",
        extra_metadata=f'''[tools.request-factory]
kind = "cargo-git"
git = "https://github.com/uw-syfi/request-factory"
rev = "{revision}"
package = "req-frontend"
bins = ["session_runner"]
''',
        entrypoint_command='"${TOOL:request-factory/session_runner}"',
    )

    package = load_evaluator_package(root)

    assert package.metadata.tools["request-factory"] == CargoGitToolSpec(
        kind="cargo-git",
        git="https://github.com/uw-syfi/request-factory",
        rev=revision,
        package="req-frontend",
        bins=("session_runner",),
    )
    assert package.command("test-check") == (tool_token("request-factory", "session_runner"),)


def test_package_command_rejects_malformed_tool_token_in_appended_arguments(
    tmp_path: Path,
) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata="""[tools.tool]
kind = "cargo-git"
git = "https://example.com/tool"
rev = "1111111111111111111111111111111111111111"
package = "package"
bins = ["runner"]
""",
    )
    package = load_evaluator_package(root)

    with pytest.raises(EvaluatorPackageError, match="complete argv element"):
        package.command("test-check", "prefix-${TOOL:tool/runner}")


@pytest.mark.parametrize(
    ("tool_metadata", "error"),
    [
        ('rev = "abc"', "full 40-character"),
        ('rev = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"', "full 40-character"),
        ("bins = []", "at least one binary"),
        ('bins = ["runner", "runner"]', "must not contain duplicates"),
    ],
)
def test_cargo_git_tool_rejects_noncanonical_fields(
    tmp_path: Path, tool_metadata: str, error: str
) -> None:
    fields = {
        "rev": 'rev = "1111111111111111111111111111111111111111"',
        "bins": 'bins = ["runner"]',
    }
    key = tool_metadata.split(" =", maxsplit=1)[0]
    fields[key] = tool_metadata
    root = _write_package(
        tmp_path / "package",
        extra_metadata=f"""[tools.tool]
kind = "cargo-git"
git = "https://example.com/tool"
{fields["rev"]}
package = "package"
{fields["bins"]}
""",
    )

    with pytest.raises(EvaluatorPackageError, match=error):
        load_evaluator_package(root)


@pytest.mark.parametrize(
    ("entrypoint", "error"),
    [
        ('"${TOOL:missing/runner}"', "undeclared tool"),
        ('"${TOOL:tool/missing}"', "undeclared binary"),
        ('"prefix-${TOOL:tool/runner}"', "complete argv element"),
    ],
)
def test_entrypoint_rejects_invalid_tool_references(
    tmp_path: Path, entrypoint: str, error: str
) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata="""[tools.tool]
kind = "cargo-git"
git = "https://example.com/tool"
rev = "1111111111111111111111111111111111111111"
package = "package"
bins = ["runner"]
""",
        entrypoint_command=entrypoint,
    )

    with pytest.raises(EvaluatorPackageError, match=error):
        load_evaluator_package(root)


@pytest.mark.parametrize(
    ("name", "version"),
    [
        ("Uppercase", "1.0.0"),
        ("vibesys-evaluator-test", "^1.0"),
        ("vibesys evaluator test", "1.0.0"),
    ],
)
def test_requirement_rejects_noncanonical_values(name: str, version: str) -> None:
    with pytest.raises(ValidationError):
        EvaluatorPackageRequirement(name=name, version=version)


def test_unknown_entrypoint_lists_available_names(tmp_path: Path) -> None:
    package = load_evaluator_package(_write_package(tmp_path / "package"))

    with pytest.raises(EvaluatorPackageError, match="available entrypoints: test-check"):
        package.command("missing")


def test_package_digest_ignores_rust_build_output(tmp_path: Path) -> None:
    root = _write_package(tmp_path / "package")
    before = load_evaluator_package(root).digest
    artifact = root / "native_runner" / "target" / "debug" / "runner"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"machine-local build output")

    assert load_evaluator_package(root).digest == before


def test_load_package_reports_missing_directory_and_metadata(tmp_path: Path) -> None:
    with pytest.raises(EvaluatorPackageError, match="evaluator package is not a directory"):
        load_evaluator_package(tmp_path / "absent")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(EvaluatorPackageError, match="evaluator package metadata not found"):
        load_evaluator_package(empty)


def test_load_package_wraps_unparseable_metadata(tmp_path: Path) -> None:
    root = tmp_path / "bad"
    root.mkdir()
    (root / "vibesys.evaluator.toml").write_text("not = [valid", encoding="utf-8")

    with pytest.raises(EvaluatorPackageError, match="invalid evaluator package metadata"):
        load_evaluator_package(root)


def test_registry_reports_missing_collection_without_available_list(tmp_path: Path) -> None:
    requirement = EvaluatorPackageRequirement(name="pkg", version="1")
    with pytest.raises(EvaluatorPackageNotFoundError, match="collection does not exist"):
        resolve_evaluator_package(tmp_path / "nowhere", requirement)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(EvaluatorPackageNotFoundError) as excinfo:
        resolve_evaluator_package(empty, requirement)
    assert "pkg==1 not found" in str(excinfo.value)
    assert "available packages" not in str(excinfo.value)


_TOOL_HEAD = """[tools.tool]
kind = "cargo-git"
git = "{git}"
rev = "1111111111111111111111111111111111111111"
package = "{package}"
bins = [{bins}]
"""


@pytest.mark.parametrize(
    ("git", "package", "bins", "error"),
    [
        ("http://example.com/tool", "package", '"runner"', "HTTPS URL without credentials"),
        ("https://user:pw@example.com/tool", "package", '"runner"', "HTTPS URL without"),
        ("https://example.com/tool?x=1", "package", '"runner"', "HTTPS URL without"),
        ("https://example.com/tool", "Bad Name", '"runner"', "canonical Cargo package name"),
        ("https://example.com/tool", "package", '"Bad Bin"', "invalid Cargo binary name"),
    ],
)
def test_cargo_git_tool_rejects_invalid_git_package_and_bins(
    tmp_path: Path, git: str, package: str, bins: str, error: str
) -> None:
    root = _write_package(
        tmp_path / "package",
        extra_metadata=_TOOL_HEAD.format(git=git, package=package, bins=bins),
    )

    with pytest.raises(EvaluatorPackageError, match=error):
        load_evaluator_package(root)


def test_metadata_rejects_duplicate_toolchains_and_bad_tool_names(tmp_path: Path) -> None:
    dup = _write_package(tmp_path / "dup", extra_metadata='toolchains = ["go", "go"]\n')
    with pytest.raises(EvaluatorPackageError, match="toolchains must not contain duplicates"):
        load_evaluator_package(dup)

    tool = _TOOL_HEAD.format(git="https://example.com/t", package="p", bins='"r"')
    bad = _write_package(
        tmp_path / "bad", extra_metadata=tool.replace("[tools.tool]", "[tools.Bad_Tool]")
    )
    with pytest.raises(EvaluatorPackageError, match="invalid evaluator tool name: 'Bad_Tool'"):
        load_evaluator_package(bad)


def _write_raw_entrypoints(root: Path, entrypoints: str) -> Path:
    root.mkdir(parents=True)
    (root / "vibesys.evaluator.toml").write_text(
        f'schema_version = 1\nname = "pkg"\nversion = "1"\nprotocol_version = 1\n{entrypoints}',
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize(
    ("entrypoints", "error"),
    [
        ("[entrypoints]\n", "entrypoints must define at least one command"),
        ('[entrypoints]\nBad_Name = ["x"]\n', "entrypoint 'Bad_Name' must contain lowercase"),
        ("[entrypoints]\nok = []\n", "entrypoint 'ok' must contain at least one argv element"),
        ('[entrypoints]\nok = ["x", ""]\n', "entrypoint 'ok' contains an empty argv element"),
        (
            '[entrypoints]\nok = ["a${PYTHON}"]\n',
            "malformed Python token; ..PYTHON. must occupy one complete argv element",
        ),
    ],
)
def test_metadata_rejects_invalid_entrypoints(tmp_path: Path, entrypoints: str, error: str) -> None:
    root = _write_raw_entrypoints(tmp_path / "pkg", entrypoints)

    with pytest.raises(EvaluatorPackageError, match=error):
        load_evaluator_package(root)


def test_a_package_declares_the_outputs_a_candidate_build_must_produce(tmp_path: Path) -> None:
    root = _write_package(
        tmp_path / "package", extra_metadata='candidate_outputs = ["lib/queue.so", "q.so"]'
    )

    assert load_evaluator_package(root).metadata.candidate_outputs == ("lib/queue.so", "q.so")


@pytest.mark.parametrize(
    "output",
    ["", "/abs/q.so", "../q.so", "lib/../q.so", "lib//q.so", "./q.so", "lib/", ".git/q.so", "a:b"],
)
def test_a_candidate_output_must_be_a_normalized_workspace_file(
    tmp_path: Path, output: str
) -> None:
    root = _write_package(tmp_path / "package", extra_metadata=f'candidate_outputs = ["{output}"]')

    with pytest.raises(EvaluatorPackageError, match="candidate"):
        load_evaluator_package(root)
