"""Process-ownership contract for the adverse-connectivity gateway fixture."""

from io import BytesIO

from tests.support.web_matrix_gateway import wait_for_owner_close


def test_owner_pipe_eof_requests_gateway_shutdown() -> None:
    """A hard worker exit closes the lease even though no finally block runs."""
    requests: list[str] = []

    owner = BytesIO()
    wait_for_owner_close(lambda: owner.read(4096), lambda: requests.append("stop"))

    assert requests == ["stop"]
