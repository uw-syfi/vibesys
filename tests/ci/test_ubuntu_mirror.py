"""Feature CI uses the runner archive without copying its Ubuntu release."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from hypothesis import example, given
from hypothesis import strategies as st
from scripts.ci_ubuntu_mirror import ubuntu_mirror

REPO = Path(__file__).resolve().parents[2]


@given(
    host=st.from_regex(r"[a-z]{1,12}\.example\.com", fullmatch=True),
    scheme=st.sampled_from(["http", "https"]),
    trailing=st.sampled_from(["", "/"]),
    deb822=st.booleans(),
)
@example(host="mirror.example.com", scheme="http", trailing="/", deb822=True)
def test_runner_archive_is_read_from_both_apt_source_formats(
    host: str, scheme: str, trailing: str, *, deb822: bool
) -> None:
    uri = f"{scheme}://{host}/ubuntu"
    source = (
        f"URIs: {uri}{trailing}\nSuites: noble noble-updates"
        if deb822
        else (f"deb [arch=amd64] {uri}{trailing} noble main universe")
    )
    sources = (
        "# deb http://ignored.example.com/ubuntu noble main\n"
        "deb http://security.ubuntu.com/ubuntu noble-security main\n"
        "deb https://vendor.example.com/packages stable main\n" + source
    )
    assert ubuntu_mirror(sources) == uri


@pytest.mark.parametrize(
    "sources",
    [
        "",
        "# URIs: http://mirror.example.com/ubuntu",
        "URIs: http://security.ubuntu.com/ubuntu",
        "URIs: https://vendor.example.com/ubuntu-tools",
        "URIs: http://user:pass@host-a/ubuntu",
    ],
)
def test_missing_runner_archive_fails_instead_of_guessing(sources: str) -> None:
    with pytest.raises(ValueError, match="no Ubuntu archive mirror"):
        ubuntu_mirror(sources)


def test_ubuntu_feature_builds_use_the_runner_mirror_and_debian_keeps_its_base() -> None:
    jobs = yaml.safe_load((REPO / ".github/workflows/test.yml").read_text())["jobs"]
    steps = jobs["feature-tests"]["steps"]
    prepare = next(
        step for step in steps if step.get("name") == "Use the runner's Ubuntu archive mirror"
    )
    assert prepare["if"] == "matrix.family != 'debian'"
    assert "python3 scripts/ci_ubuntu_mirror.py" in prepare["run"]
    assert '--build-arg "UBUNTU_MIRROR=${mirror}"' in prepare["run"]
    assert '--build-arg "BASE_IMAGE=${BASE_IMAGE}"' in prepare["run"]
    assert "--tag vibesys-feature-base:ci" in prepare["run"]
    test = next(step for step in steps if "test_features.sh" in step.get("run", ""))
    assert steps.index(prepare) < steps.index(test)
    assert test["env"]["FEATURE_BASE"] == (
        "${{ matrix.family == 'debian' && matrix.base || 'vibesys-feature-base:ci' }}"
    )
    assert 'test_features.sh "$FEATURE_BASE"' in test["run"]
    cache = next(step for step in steps if step.get("id") == "docker-cache")
    assert "scripts/ci_ubuntu_mirror.py" in cache["with"]["key"]


def test_mirror_layer_preserves_release_and_never_refreshes_package_lists() -> None:
    dockerfile = (
        REPO / "libs/vs-sandbox/src/vs_sandbox/devcontainer-cli/UbuntuMirror.Dockerfile"
    ).read_text()
    assert "FROM ${BASE_IMAGE}" in dockerfile
    assert "ARG UBUNTU_MIRROR" in dockerfile
    assert "archive[.]ubuntu[.]com|security[.]ubuntu[.]com" in dockerfile
    assert "${UBUNTU_MIRROR}/@g" in dockerfile
    assert "-name '*.list' -o -name '*.sources'" in dockerfile
    assert "apt-get" not in dockerfile
    assert "noble" not in dockerfile
    assert "jammy" not in dockerfile
