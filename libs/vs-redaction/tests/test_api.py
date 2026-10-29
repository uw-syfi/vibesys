"""Public contract tests for diagnostic redaction."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_redaction.api import (
    HOST_PATH_PLACEHOLDER,
    DiagnosticRedactor,
    mask_host_paths,
    redact_diagnostic_tail,
    redact_diagnostic_text,
)


@pytest.mark.parametrize(
    ("credential", "key_length"),
    [
        ("API_TOKEN=assignment-secret", len("API_TOKEN")),
        ("Bearer bearer-secret", len("Bearer")),
    ],
)
def test_diagnostic_tail_never_exposes_a_credential_split_by_its_boundary(
    credential: str, key_length: int
) -> None:
    limit = 1000

    for split_at in range(1, key_length):
        suffix = "z" * (limit + split_at - len(credential) - 1)
        tail = redact_diagnostic_tail("prefix:" + credential + " " + suffix, max_chars=limit)

        assert len(tail) == limit
        assert "secret" not in tail


@pytest.mark.parametrize(
    "credential",
    [
        "API_TOKEN=assignment-secret",
        "Bearer bearer-secret",
        "clientSecret=camel-secret",
    ],
)
def test_complete_diagnostic_text_redacts_common_credentials(credential: str) -> None:
    assert "secret" not in redact_diagnostic_text(credential)


def test_diagnostic_tail_redacts_before_selecting_the_tail() -> None:
    text = "prefix:API_TOKEN=" + "s" * 2_000 + " " + "z" * 1_000

    tail = redact_diagnostic_tail(text, max_chars=1_100)

    assert "API_TOKEN=[REDACTED]" in tail
    assert "s" * 20 not in tail


def test_diagnostic_tail_rejects_a_negative_bound() -> None:
    with pytest.raises(ValueError, match="max_chars must be non-negative"):
        redact_diagnostic_tail("TOKEN=secret", max_chars=-1)


def test_zero_length_diagnostic_tail_is_empty() -> None:
    assert redact_diagnostic_tail("TOKEN=secret", max_chars=0) == ""


@pytest.mark.parametrize(
    "assignment",
    [
        "AWS_ACCESS_KEY_ID=access-id",
        "AWS_SECRET_ACCESS_KEY=access-secret",
        "OPENAI_API_KEY=openai-secret",
        "GITHUB_TOKEN=github-secret",
        "CLIENTSECRET=client-secret",
        "PRIVATEKEY=private-key",
        "DBPASSWORD=database-password",
        "ACCESSTOKEN=access-token",
        "AWSSECRETACCESSKEY=aws-secret",
        "clientSecret=camel-secret",
        "privateKey=camel-key",
        "dbPassword=camel-password",
        "accessToken=camel-token",
        "service-password=hyphen-password",
    ],
)
def test_common_credential_names_are_redacted(assignment: str) -> None:
    assert redact_diagnostic_text(assignment).endswith("=[REDACTED]")


@pytest.mark.parametrize(
    "assignment",
    [
        "monkey=banana",
        "MONKEY=banana",
        "donkey=carrot",
        "turkey=dinner",
        "hockey=sport",
        "keyboard=qwerty",
        "keynote=slides",
        "secretary=person",
        "tokenizer=parser",
        "passwordless=enabled",
        "api_keynote=slides",
    ],
)
def test_ordinary_words_are_not_treated_as_credentials(assignment: str) -> None:
    assert redact_diagnostic_text(assignment) == assignment


def test_repeated_bounds_never_reintroduce_a_split_credential() -> None:
    text = "prefix:Bearer repeated-bound-secret " + "z" * 2_000

    first = redact_diagnostic_tail(text, max_chars=1_500)
    second = redact_diagnostic_tail(first, max_chars=1_000)

    assert len(second) == 1_000
    assert "repeated-bound-secret" not in second


@pytest.mark.parametrize(
    "text",
    [
        "prefix API_TOKEN\n =\nassignment-secret suffix",
        "prefix bearer\n bearer-secret suffix",
        "prefix-clientSecret:camel-secret, suffix",
        "ordinary output without credentials",
    ],
)
def test_stream_redaction_matches_complete_redaction_at_every_split(text: str) -> None:
    expected = redact_diagnostic_text(text)

    for split_at in range(len(text) + 1):
        redactor = DiagnosticRedactor()
        actual = redactor.append(text[:split_at])
        actual += redactor.append(text[split_at:])
        actual += redactor.finish()

        assert actual == expected

    redactor = DiagnosticRedactor()
    one_character_at_a_time = "".join(redactor.append(character) for character in text)
    assert one_character_at_a_time + redactor.finish() == expected


_COMPONENT = st.text(alphabet="abcdefghij0123456789_-", min_size=1, max_size=6)
_PATHS = st.lists(_COMPONENT, min_size=1, max_size=5).map(lambda parts: "/" + "/".join(parts))


@given(
    paths=st.lists(_PATHS, min_size=1, max_size=4),
    tails=st.lists(st.sampled_from(["", "/lib/x.py", "/bin/python", ":"]), min_size=4, max_size=4),
    noise=st.sampled_from([" ", "\n", ": "]),
)
@example(paths=["/work1/user/venv/bin/python"], tails=["", "", "", ""], noise=" ")
def test_masked_text_names_none_of_the_operators_paths(
    paths: list[str], tails: list[str], noise: str
) -> None:
    text = noise.join(path + tail for path, tail in zip(paths, tails, strict=False))
    masked = mask_host_paths(text, paths)

    assert all(path not in masked for path in paths)
    assert mask_host_paths(masked, paths) == masked


@given(text=st.text(alphabet="ABC xyz:\n._", max_size=40), paths=st.lists(_PATHS, max_size=3))
def test_text_without_a_private_path_is_unchanged(text: str, paths: list[str]) -> None:
    assert mask_host_paths(text, paths) == text


def test_the_directory_every_path_shares_is_hidden_with_what_lies_under_it() -> None:
    paths = ["/cluster/work/u/venv/bin/python", "/cluster/work/u/runs"]
    traceback = "File /cluster/work/u/venv/lib/python3.10/site-packages/x.py, in main"

    assert mask_host_paths(traceback, paths) == (
        f"File {HOST_PATH_PLACEHOLDER}/venv/lib/python3.10/site-packages/x.py, in main"
    )


def test_a_path_is_matched_only_up_to_a_component_boundary() -> None:
    assert mask_host_paths("/a/b/c and /a/bc", ["/a/b/c"]) == f"{HOST_PATH_PLACEHOLDER} and /a/bc"
