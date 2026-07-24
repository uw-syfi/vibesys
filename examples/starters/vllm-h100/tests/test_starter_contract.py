from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - compatibility for older pytest launchers.
    import tomli as tomllib


def test_starter_dependency_contract() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    dependencies = set(pyproject["project"]["dependencies"])

    assert "vllm==0.10.0" in dependencies
    assert any(dep.startswith("modal>=") for dep in dependencies)
    assert any(dep.startswith("fastapi>=") for dep in dependencies)
    assert any(dep.startswith("pydantic>=") for dep in dependencies)
    assert any(dep.startswith("uvicorn>=") for dep in dependencies)


def test_pytest_does_not_collect_materialized_vllm_source() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    pytest_options = pyproject["tool"]["pytest"]["ini_options"]

    assert pytest_options["testpaths"] == ["tests"]
    assert "vllm" in pytest_options["norecursedirs"]


def test_modal_bridge_url_parser_handles_wrapped_output() -> None:
    from serve import _extract_modal_url

    wrapped = """
    Web Function URL for Server.api =>
      https://vibeserve--vibesys-20260722-071702-008413-3a1109b0-v-7babfa-dev.moda
      l.run (label truncated)
    """

    assert (
        _extract_modal_url(wrapped)
        == "https://vibeserve--vibesys-20260722-071702-008413-3a1109b0-v-7babfa-dev.modal.run"
    )
