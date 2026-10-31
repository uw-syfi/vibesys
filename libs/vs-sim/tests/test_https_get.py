"""The HTTPS client takes a whole 200 body from an https address or fails; its Fake keeps the rules."""

from __future__ import annotations

import io
import urllib.request
from http.client import HTTPMessage

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import HttpsGetError, HttpsOnlyRedirects, UrllibHttpsGet
from vs_sim.api.testing import FakeHttpsGet


@given(address=st.from_regex(r"\A(http|ftp|file|)[:/a-z0-9.]{0,30}\Z", fullmatch=True))
def test_an_address_that_is_not_https_is_refused_by_both_before_any_connection(
    address: str,
) -> None:
    """The real client is asked with no network here: a refusal is the only thing it can do."""
    for client in (UrllibHttpsGet(), FakeHttpsGet(b"body")):
        with pytest.raises(HttpsGetError):
            client.get(address, accept="application/json", timeout=1.0, max_bytes=100)


def test_the_fake_answers_in_the_order_scripted_and_fails_when_the_script_runs_out() -> None:
    client = FakeHttpsGet(b"first", HttpsGetError("down"), b"third")
    assert client.get("https://a", accept="x", timeout=1.0, max_bytes=100) == b"first"
    with pytest.raises(HttpsGetError, match="down"):
        client.get("https://a", accept="x", timeout=1.0, max_bytes=100)
    assert client.get("https://b", accept="y", timeout=1.0, max_bytes=100) == b"third"
    with pytest.raises(HttpsGetError):
        client.get("https://c", accept="z", timeout=1.0, max_bytes=100)
    assert client.requests == [
        ("https://a", "x"),
        ("https://a", "x"),
        ("https://b", "y"),
        ("https://c", "z"),
    ]


@given(body=st.binary(max_size=64), limit=st.integers(min_value=1, max_value=64))
def test_a_body_over_the_limit_is_an_error_not_a_cut_document(body: bytes, limit: int) -> None:
    client = FakeHttpsGet(body)
    if len(body) > limit:
        with pytest.raises(HttpsGetError):
            client.get("https://a", accept="x", timeout=1.0, max_bytes=limit)
    else:
        assert client.get("https://a", accept="x", timeout=1.0, max_bytes=limit) == body


@given(
    target=st.from_regex(r"\A(http|ftp|file)://[a-z0-9.]{1,20}(/[a-z0-9]{0,10})?\Z", fullmatch=True)
)
def test_a_redirect_to_anything_but_https_is_refused(target: str) -> None:
    """A response cannot take the fetch off TLS: the redirect is an error, not followed."""
    request = urllib.request.Request("https://prices.example/list")
    with pytest.raises(HttpsGetError, match="non-https"):
        HttpsOnlyRedirects().redirect_request(
            request, io.BytesIO(), 302, "Found", HTTPMessage(), target
        )


@given(host=st.from_regex(r"\A[a-z]{1,10}\.example\Z", fullmatch=True))
def test_a_redirect_to_another_https_address_is_followed(host: str) -> None:
    request = urllib.request.Request("https://prices.example/list")
    followed = HttpsOnlyRedirects().redirect_request(
        request, io.BytesIO(), 302, "Found", HTTPMessage(), f"https://{host}/list"
    )
    assert followed is not None
    assert followed.full_url == f"https://{host}/list"
