"""endpoint(): the candidate server's URL from VIBESYS_SERVICE_URL, or a clear refusal."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys_evaluator.llm_serving import endpoint

hosts = st.from_regex(r"[a-z][a-z0-9-]{0,20}", fullmatch=True) | st.just("127.0.0.1")


@given(host=hosts, port=st.integers(1, 65535), slashes=st.integers(0, 2))
def test_the_url_comes_back_without_a_trailing_slash(host: str, port: int, slashes: int) -> None:
    with pytest.MonkeyPatch.context() as env:
        env.setenv("VIBESYS_SERVICE_URL", f"http://{host}:{port}" + "/" * slashes)
        assert endpoint() == f"http://{host}:{port}"


@pytest.mark.parametrize("value", ["", "   "])
def test_an_unset_url_names_the_variable_and_who_sets_it(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("VIBESYS_SERVICE_URL", value)
    with pytest.raises(Exception, match=r"VIBESYS_SERVICE_URL is not set: vibesys-cli sets it"):
        endpoint()


def test_an_absent_variable_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIBESYS_SERVICE_URL", raising=False)
    with pytest.raises(Exception, match="VIBESYS_SERVICE_URL is not set"):
        endpoint()


@pytest.mark.parametrize(
    "value", ["127.0.0.1:8000", "ftp://host:21", "http://", "http://host:8000/v1", "http://a b"]
)
def test_a_url_that_is_not_a_base_url_is_refused(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("VIBESYS_SERVICE_URL", value)
    with pytest.raises(Exception, match="is not an http"):
        endpoint()
