"""Public contract tests for diagnostic redaction."""

import pytest

from vs_redaction.api import redact_diagnostic_tail, redact_diagnostic_text


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
