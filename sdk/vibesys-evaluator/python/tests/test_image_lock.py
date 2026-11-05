"""The llm-serving image's lock agrees with the package: one version, one harness pin.

``image/llm-serving/uv.lock`` pins the image's Python environment and
``requirements.txt`` beside it is what the image installs (``uv export`` of
the lock). A pin changed in one place and not the others would build an image
whose harness the package refuses, or whose package is not this version.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from vibesys_evaluator.llm_serving import agentx

SDK = Path(__file__).resolve().parents[2]
LOCK_DIR = SDK / "image" / "llm-serving"
PACKAGE = SDK / "python" / "pyproject.toml"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", re.MULTILINE)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock() -> dict[str, dict]:
    lock = tomllib.loads((LOCK_DIR / "uv.lock").read_text())
    return {_canonical(p["name"]): p for p in lock["package"]}


def test_the_lock_pins_the_harness_the_package_replays_with() -> None:
    assert _lock()["agentx-harness"]["version"] == agentx.HARNESS_VERSION
    project = tomllib.loads((LOCK_DIR / "pyproject.toml").read_text())["project"]
    assert f"agentx-harness=={agentx.HARNESS_VERSION}" in project["dependencies"]


def test_the_lock_holds_this_version_of_the_package_and_its_pins() -> None:
    package = tomllib.loads(PACKAGE.read_text())["project"]
    locked = _lock()
    assert locked["vibesys-evaluator"]["version"] == package["version"]
    for requirement in package["dependencies"]:
        name, version = requirement.split("==")
        assert locked[_canonical(name)]["version"] == version, requirement


def test_requirements_txt_is_the_export_of_the_lock() -> None:
    exported = dict(_PIN.findall((LOCK_DIR / "requirements.txt").read_text()))
    exported = {_canonical(name): version for name, version in exported.items()}
    locked = {
        name: package["version"]
        for name, package in _lock().items()
        if "registry" in package.get("source", {})
    }
    assert exported == locked
    assert "--hash=sha256:" in (LOCK_DIR / "requirements.txt").read_text()
