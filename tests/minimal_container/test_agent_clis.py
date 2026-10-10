"""The agent CLIs start in the editor container of every default base image.

``claude --version`` proves the binary and its Node runtime run as the container
user; the Codex app-server handshake proves the long-lived transport process starts
and answers JSON-RPC, which is what the stream transport needs of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.minimal_container.stdio import StdioJsonProcess

if TYPE_CHECKING:
    from tests.minimal_container.editor import Editor

pytestmark = pytest.mark.minimal_container


def test_claude_prints_its_version(editor: Editor) -> None:
    status, output = editor.run("claude --version")

    assert status == 0, output
    assert "Claude Code" in output


def test_codex_answers_the_app_server_handshake(editor: Editor) -> None:
    with StdioJsonProcess(editor.argv(["codex", "app-server"])) as server:
        server.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "minimal-container-tier", "version": "0"}},
            }
        )
        reply = server.reply_to(1)

    assert "error" not in reply, reply
    assert "userAgent" in reply["result"], reply
