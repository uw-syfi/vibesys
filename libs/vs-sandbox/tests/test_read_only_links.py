"""Operator read-only paths are rejected at startup when a symlink in them would dangle in a job."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import HostResource
from vs_sandbox.api.slurm import ReadOnlyPathLinkError, check_read_only_links

if TYPE_CHECKING:
    from pathlib import Path

_KEY = "vibesys.agent_gpu.read_only_paths"
# Linux binds a listed symlink as what it points to; Seatbelt checks the resolved path.
_LINUX_ONLY = pytest.mark.skipif(sys.platform == "darwin", reason="Linux bind semantics")


def _check(tmp_path: Path, listed: list[Path]) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    paths = [str(path) for path in listed]
    check_read_only_links(
        _KEY, paths, workspace=workspace, resources=[HostResource(path) for path in listed]
    )


def _interpreter(directory: Path) -> Path:
    (directory / "bin").mkdir(parents=True)
    real = directory / "bin" / "python3.12"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    return real


def test_a_venv_alone_is_rejected_naming_the_link_and_the_path_to_add(tmp_path: Path) -> None:
    real = _interpreter(tmp_path / "python-install")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").symlink_to(real)

    with pytest.raises(ReadOnlyPathLinkError) as raised:
        _check(tmp_path, [tmp_path / "venv"])

    message = str(raised.value)
    assert _KEY in message
    assert f"{tmp_path / 'venv'}: {tmp_path / 'venv' / 'bin' / 'python'} -> {real}" in message
    assert f"add {real} (or the directory that holds it) to {_KEY}" in message


def test_the_install_listed_too_makes_the_venv_acceptable(tmp_path: Path) -> None:
    real = _interpreter(tmp_path / "python-install")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").symlink_to(real)

    _check(tmp_path, [tmp_path / "venv", tmp_path / "python-install"])


def _alias_layout(tmp_path: Path, *, relative: bool) -> Path:
    """A venv whose python links to an alias directory that links to the install (uv's layout)."""
    real = tmp_path / "store" / "cpython-3.12.15"
    _interpreter(real)
    alias = tmp_path / "store" / "cpython-3.12"
    alias.symlink_to(real.name if relative else real)
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    target = alias / "bin" / "python3.12"
    (tmp_path / "venv" / "bin" / "python").symlink_to(
        "../../store/cpython-3.12/bin/python3.12" if relative else target
    )
    return tmp_path / "venv"


@pytest.mark.parametrize("relative", [True, False])
def test_a_link_through_an_alias_directory_names_the_alias_not_the_install(
    tmp_path: Path, *, relative: bool
) -> None:
    venv = _alias_layout(tmp_path, relative=relative)

    with pytest.raises(ReadOnlyPathLinkError) as raised:
        _check(tmp_path, [venv])

    assert f"add {tmp_path / 'store' / 'cpython-3.12' / 'bin' / 'python3.12'} " in str(raised.value)


@_LINUX_ONLY
@pytest.mark.parametrize("relative", [True, False])
def test_listing_the_alias_is_enough_because_a_listed_link_is_bound_as_what_it_points_to(
    tmp_path: Path, *, relative: bool
) -> None:
    venv = _alias_layout(tmp_path, relative=relative)

    _check(tmp_path, [venv, tmp_path / "store" / "cpython-3.12"])


@pytest.mark.parametrize("listed_the_install", [False, True])
def test_listing_only_the_install_behind_an_alias_still_rejects_the_alias_path(
    tmp_path: Path, *, listed_the_install: bool
) -> None:
    venv = _alias_layout(tmp_path, relative=False)
    listed = [venv, tmp_path / "store" / "cpython-3.12.15"] if listed_the_install else [venv]

    with pytest.raises(ReadOnlyPathLinkError, match=r"cpython-3\.12/bin/python3\.12"):
        _check(tmp_path, listed)


def test_a_relative_link_to_a_sibling_inside_the_listed_tree_is_accepted(tmp_path: Path) -> None:
    real = _interpreter(tmp_path / "venv")
    (tmp_path / "venv" / "bin" / "python").symlink_to(real.name)
    (tmp_path / "venv" / "bin" / "python3").symlink_to("python")

    _check(tmp_path, [tmp_path / "venv"])


def test_a_relative_link_climbing_out_of_the_listed_tree_is_rejected(tmp_path: Path) -> None:
    _interpreter(tmp_path / "elsewhere")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").symlink_to("../../elsewhere/bin/python3.12")

    with pytest.raises(ReadOnlyPathLinkError, match=r"elsewhere/bin/python3\.12"):
        _check(tmp_path, [tmp_path / "venv"])


def test_a_link_to_a_system_program_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "sh").symlink_to("/bin/sh")

    _check(tmp_path, [tmp_path / "venv"])


def test_a_broken_link_inside_the_listed_tree_is_rejected_as_broken(tmp_path: Path) -> None:
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").symlink_to("missing")

    with pytest.raises(ReadOnlyPathLinkError, match="is broken"):
        _check(tmp_path, [tmp_path / "venv"])


def test_a_link_loop_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "a").symlink_to("b")
    (tmp_path / "venv" / "bin" / "b").symlink_to("a")

    with pytest.raises(ReadOnlyPathLinkError, match="loops"):
        _check(tmp_path, [tmp_path / "venv"])


def test_only_links_in_bin_directories_are_followed(tmp_path: Path) -> None:
    (tmp_path / "venv" / "share").mkdir(parents=True)
    (tmp_path / "venv" / "share" / "docs").symlink_to(tmp_path / "unlisted")
    (tmp_path / "venv" / "share" / "dangling").symlink_to("missing")

    _check(tmp_path, [tmp_path / "venv"])


def test_a_link_in_a_nested_bin_directory_is_followed(tmp_path: Path) -> None:
    (tmp_path / "venv" / "lib" / "tool" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "lib" / "tool" / "bin" / "x").symlink_to(tmp_path / "unlisted")

    with pytest.raises(ReadOnlyPathLinkError, match="unlisted"):
        _check(tmp_path, [tmp_path / "venv"])


def test_every_offending_link_is_reported_at_once(tmp_path: Path) -> None:
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "a").symlink_to(tmp_path / "one")
    (tmp_path / "venv" / "bin" / "b").symlink_to(tmp_path / "two")

    with pytest.raises(ReadOnlyPathLinkError) as raised:
        _check(tmp_path, [tmp_path / "venv"])

    assert "bin/a" in str(raised.value)
    assert "bin/b" in str(raised.value)


@_LINUX_ONLY
@given(
    chain=st.lists(st.booleans(), min_size=1, max_size=4),
    listed_mask=st.lists(st.booleans(), min_size=4, max_size=4),
)
def test_a_chain_of_links_is_accepted_exactly_when_its_first_hop_is_listed(
    tmp_path_factory: pytest.TempPathFactory, chain: list[bool], listed_mask: list[bool]
) -> None:
    """Each ``True`` hop is an absolute link, each ``False`` a relative one; the last is a file."""
    root = tmp_path_factory.mktemp("chain")
    store = root / "store"
    store.mkdir()
    nodes = [store / f"n{index}" for index in range(len(chain))]
    (store / "final").write_text("x", encoding="utf-8")
    successors = [*nodes[1:], store / "final"]
    for node, successor, absolute in zip(nodes, successors, chain, strict=True):
        node.symlink_to(successor if absolute else successor.name)
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").symlink_to(nodes[0])
    listed = [root / "venv", *(node for node, on in zip(nodes, listed_mask, strict=False) if on)]

    if listed_mask[0]:
        _check(root, listed)
    else:
        with pytest.raises(ReadOnlyPathLinkError):
            _check(root, listed)
