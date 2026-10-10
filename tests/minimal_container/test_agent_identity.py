"""The agent user of the editor container is whole on every base image.

Regression for #1589: a base image that already owns uid 1000 under another name
(Ubuntu 24.04 names it ``ubuntu``) had its user renamed to ``agent`` but kept the
old group name, so the sandbox's ``groupmod ... agent`` found no such group on a
host whose gid is not 1000. This runs on every default base image and on a small
stand-in that has the property, so a host without that large image still covers it.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from tests.minimal_container.conftest import expect_failure_for

if TYPE_CHECKING:
    from tests.minimal_container.editor import Editor

pytestmark = pytest.mark.minimal_container

#: Also run on the stand-in bases (see ``pytest_generate_tests``).
INCLUDE_STAND_INS = True


def test_the_agent_user_has_a_group_of_its_own_name(
    editor: Editor, request: pytest.FixtureRequest
) -> None:
    expect_failure_for(request, editor, ("cuda", "rocm", "ubuntu-uid-1000"), 1589)

    status, output = editor.run("id -un; id -gn; getent group agent | cut -d: -f1")

    assert status == 0, output
    assert output.split() == ["agent", "agent", "agent"]


def test_the_agent_user_has_the_ids_of_the_host_user(editor: Editor) -> None:
    status, output = editor.run("id -u; id -g")

    assert status == 0, output
    assert output.split() == [str(os.getuid()), str(os.getgid())]
