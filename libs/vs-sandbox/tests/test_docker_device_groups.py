"""Device-node groups are only added to a container that forwards device nodes.

Regression for #1587: the ROCm backend added the ``video`` and ``render`` groups
even when no devices were forwarded, and an image without them failed to start.
The sandbox now refuses that combination, whichever backend builds it.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import DockerSandbox

_names = st.lists(st.text(alphabet="abcdefgz", min_size=1, max_size=8), min_size=1, max_size=3)
_devices = st.lists(st.sampled_from(["/dev/kfd", "/dev/dri/renderD128"]), min_size=1, max_size=2)


@given(groups=_names, devices=st.sampled_from([None, []]))
def test_groups_without_devices_are_refused(groups: list[str], devices: list[str] | None) -> None:
    with pytest.raises(ValueError, match="no devices are forwarded"):
        DockerSandbox(host_workspace="/w", image="img", group_add=groups, devices=devices)


@given(groups=_names, devices=_devices)
def test_groups_with_devices_are_accepted(groups: list[str], devices: list[str]) -> None:
    DockerSandbox(host_workspace="/w", image="img", group_add=groups, devices=devices)


@given(devices=st.none() | _devices)
def test_no_groups_is_always_accepted(devices: list[str] | None) -> None:
    DockerSandbox(host_workspace="/w", image="img", devices=devices)
