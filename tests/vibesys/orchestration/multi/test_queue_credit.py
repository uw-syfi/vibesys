"""The microbench time limit moves later by the GPU queue wait that the broker records."""

from __future__ import annotations

import asyncio

import pytest

from vibesys.orchestration.multi.turns import _queue_wait, _wait_with_queue_credit


def test_queue_wait_reads_the_file_or_zero(tmp_path):
    path = tmp_path / "queue_wait_s"
    assert _queue_wait("") == 0.0
    assert _queue_wait(str(path)) == 0.0
    path.write_text("42\n")
    assert _queue_wait(str(path)) == 42.0
    path.write_text("garbage")
    assert _queue_wait(str(path)) == 0.0


def test_limit_without_queue_wait_times_out_and_cancels(tmp_path):
    cancelled = []

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_wait_with_queue_credit(slow(), 0.2, str(tmp_path / "none"), poll=0.05))
    assert cancelled


def test_queue_wait_during_the_call_extends_the_limit(tmp_path):
    path = tmp_path / "queue_wait_s"
    path.write_text("100")  # wait before the call does not count

    async def body():
        await asyncio.sleep(0.1)
        path.write_text("101")  # one second of queue wait during the call
        await asyncio.sleep(0.5)
        return "done"

    assert asyncio.run(_wait_with_queue_credit(body(), 0.3, str(path), poll=0.05)) == "done"


def test_no_limit_waits_for_the_result():
    async def body():
        return 7

    assert asyncio.run(_wait_with_queue_credit(body(), None, "")) == 7
