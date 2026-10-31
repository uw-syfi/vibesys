"""Service ownership and readiness through the injected process boundary."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

spec = importlib.util.spec_from_file_location(
    "service", Path(__file__).resolve().parents[1] / "scripts/service.py"
)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class Host:
    def __init__(self, ready_at: int, *, fail_client: bool) -> None:
        self.ready_at = ready_at
        self.fail_client = fail_client
        self.probes = 0
        self.running = False
        self.clients = 0

    def start(self, argv: list[str]) -> object:
        assert argv == ["server"]
        self.running = True
        return self

    def probe(self, handle: object, argv: list[str], timeout: float) -> bool:
        assert handle is self
        assert argv == ["ready"]
        assert timeout > 0
        self.probes += 1
        return self.probes >= self.ready_at

    def client(self, argv: list[str], timeout: float) -> int:
        assert self.running
        assert argv == ["check"]
        assert timeout > 0
        self.clients += 1
        if self.fail_client:
            raise service.ServiceError(service.Reason.CLIENT)
        return 7

    def stop(self, handle: object, timeout: float) -> None:
        assert handle is self
        assert timeout > 0
        self.running = False


@given(ready_at=st.integers(min_value=1, max_value=60), fail_client=st.booleans())
def test_client_requires_readiness_and_all_exits_stop_group(
    ready_at: int, *, fail_client: bool
) -> None:
    host = Host(ready_at, fail_client=fail_client)
    if ready_at > 50 or fail_client:
        with pytest.raises(service.ServiceError, match=r"exhausted|client failed"):
            service.serve(["server"], ["ready"], ["check"], host)
    else:
        assert service.serve(["server"], ["ready"], ["check"], host) == 7
    assert not host.running
    assert host.probes == min(ready_at, 50)
    assert host.clients == int(ready_at <= 50)
