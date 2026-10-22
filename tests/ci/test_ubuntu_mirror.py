"""Feature CI uses the runner archive without copying its Ubuntu release."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from hypothesis import example, given
from hypothesis import strategies as st
from scripts.ci_ubuntu_mirror import runner_ubuntu_mirror, ubuntu_mirror

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
def test_missing_runner_archive_keeps_image_defaults(sources: str) -> None:
    assert ubuntu_mirror(sources) is None


def test_only_cuda_feature_builds_use_the_runner_mirror() -> None:
    jobs = yaml.safe_load((REPO / ".github/workflows/test.yml").read_text())["jobs"]
    steps = jobs["feature-tests"]["steps"]
    prepare = next(
        step for step in steps if step.get("name") == "Use the runner's Ubuntu archive mirror"
    )
    assert prepare["if"] == "matrix.family == 'cuda-ubuntu'"
    assert "python3 scripts/ci_ubuntu_mirror.py" in prepare["run"]
    assert '--build-arg "UBUNTU_MIRROR=${mirror}"' in prepare["run"]
    assert '--build-arg "BASE_IMAGE=${BASE_IMAGE}"' in prepare["run"]
    assert "--tag vibesys-feature-base:ci" in prepare["run"]
    test = next(step for step in steps if "test_features.sh" in step.get("run", ""))
    assert steps.index(prepare) < steps.index(test)
    assert test["env"]["FEATURE_BASE"] == (
        "${{ matrix.family == 'cuda-ubuntu' && 'vibesys-feature-base:ci' || matrix.base }}"
    )
    assert 'test_features.sh "$FEATURE_BASE"' in test["run"]
    cache = next(step for step in steps if step.get("id") == "docker-cache")
    assert "scripts/ci_ubuntu_mirror.py" in cache["with"]["key"]


def test_mirror_layer_preserves_release_and_never_refreshes_package_lists() -> None:
    dockerfile = (
        REPO / "libs/vs-sandbox/src/vs_sandbox/devcontainer-cli/UbuntuMirror.Dockerfile"
    ).read_text()
    assert "FROM ${BASE_IMAGE}" in dockerfile
    assert 'ARG UBUNTU_MIRROR=""' in dockerfile
    assert 'if [ -z "${UBUNTU_MIRROR}" ]; then exit 0; fi;' in dockerfile
    assert "archive[.]ubuntu[.]com|security[.]ubuntu[.]com" in dockerfile
    assert "${UBUNTU_MIRROR}/@g" in dockerfile
    assert "-name '*.list' -o -name '*.sources'" in dockerfile
    assert "apt-get" not in dockerfile
    assert "noble" not in dockerfile
    assert "jammy" not in dockerfile


# Unmodified apt files copied from official Ubuntu amd64 OCI images:
# 22.04 sha256:98510ac07bb4545e389be502a8918b385c018bfe4ceeb5ac7e0375c782268b04
# 24.04 sha256:a2d43bffc46670d7108bb3c0468c67c5557c4c92e83e1e34cbff1322ad1b94dc
# The mirror list and transformation below come from actions/runner-images:
# https://github.com/actions/runner-images/blob/416ad027415129c880fc76669a2c063b0a39b4a0/images/ubuntu/scripts/build/configure-apt-sources.sh
@pytest.mark.parametrize(
    ("version", "filename"), [("22", "sources.list"), ("24", "ubuntu.sources")]
)
@pytest.mark.parametrize("runner_mirror_list", [False, True])
def test_real_ubuntu_apt_configuration(
    tmp_path: Path, version: str, filename: str, *, runner_mirror_list: bool
) -> None:
    fixtures = Path(__file__).parent / "fixtures" / "apt"
    content = (fixtures / f"ubuntu{version}" / filename).read_text()
    apt = tmp_path / "apt"
    sources = apt / "sources.list.d"
    sources.mkdir(parents=True)
    if runner_mirror_list:
        content = content.replace(
            "http://archive.ubuntu.com/ubuntu/", "mirror+file:/etc/apt/apt-mirrors.txt"
        )
        (apt / "apt-mirrors.txt").write_bytes((fixtures / "apt-mirrors.txt").read_bytes())
    if version == "24":
        (apt / "sources.list").write_text(
            "# Ubuntu sources moved to sources.list.d/ubuntu.sources\n"
        )
        (sources / filename).write_text(content)
    else:
        (apt / filename).write_text(content)
    expected = (
        "http://azure.archive.ubuntu.com/ubuntu"
        if runner_mirror_list
        else "http://archive.ubuntu.com/ubuntu"
    )
    assert runner_ubuntu_mirror(apt) == expected


@pytest.mark.parametrize("content", [None, "", "# No configured Ubuntu archive\n"])
def test_runner_without_archive_warns_and_keeps_image_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str | None
) -> None:
    if content is not None:
        (tmp_path / "sources.list").write_text(content)
    assert runner_ubuntu_mirror(tmp_path) == ""
    assert "WARNING" in caplog.text
    assert "keeping image defaults" in caplog.text
