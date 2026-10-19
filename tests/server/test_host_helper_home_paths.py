"""A helper's error messages name paths under the account's home as ``~/...``.

A client shows an error to a person who may not own the account, so the user name in the home
directory must not reach it. The channel rewrites every error it sends (``without_home``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.host_helper_support import (
    HOME,
    ByteBuffer,
    ChannelClient,
    fake_host,
    serve_on,
)

from server.host_api import HelperFeature
from server.host_helper_requests import home_relative
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Sequence

_SEGMENTS = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)


def _ask(requests: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))

    def main() -> list[dict[str, Any]]:
        client = ChannelClient(threads)
        worker = serve_on(threads, client, ByteBuffer(), host)
        client.send(
            {
                "type": "hello",
                "request_id": "h",
                "deadline_ms": 5000,
                "client_versions": [1, 1],
                "features": [feature.value for feature in HelperFeature],
                "app_version": "test",
            }
        )
        client.receive()
        replies = []
        for request in requests:
            client.send({"deadline_ms": 5000, **request})
            replies.append(client.receive())
        client.close()
        worker.join(HANG_GUARD_S)
        return replies

    return threads.run(main)


@settings(max_examples=examples(pr=30, nightly=200), deadline=None)
@given(parts=st.lists(_SEGMENTS, min_size=1, max_size=4), tail=st.sampled_from(["", ".", " x"]))
def test_a_path_under_home_is_written_with_a_tilde(parts: list[str], tail: str) -> None:
    path = "/".join(parts)

    assert home_relative(f"no {HOME}/{path}{tail}", HOME) == f"no ~/{path}{tail}"
    assert home_relative(f"in {HOME}", HOME) == "in ~"


@given(sibling=_SEGMENTS)
def test_a_path_that_only_starts_like_home_is_left_alone(sibling: str) -> None:
    other = f"{HOME}{sibling}/x"

    assert home_relative(other, HOME) == other
    assert home_relative(other, "/") == other


def test_a_missing_project_and_an_unlistable_folder_name_no_home() -> None:
    replies = _ask(
        [
            {"type": "tasks.list", "request_id": "t", "project": "~/gone"},
            {"type": "paths.complete", "request_id": "c", "prefix": "~/nowhere/"},
        ]
    )

    assert [reply["type"] for reply in replies] == ["error", "error"]
    assert all(HOME not in reply["message"] for reply in replies)
    assert "~/nowhere" in replies[1]["message"]
