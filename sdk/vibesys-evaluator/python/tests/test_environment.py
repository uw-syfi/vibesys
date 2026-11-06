"""What a job is told about itself: the codec reads the corpus vibesys-cli's Go codec is held to.

The corpus (``proto/vibesys/cli/v1/conformance/environment.json``) is the contract between the
two codecs; the properties say that any valid message is written and read back unchanged and that
a bad value is refused naming its variable and never repeating the value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

# test-isolation: the codec is private to the package (the job's environment is read by the
# functions that need it); its behavior is the contract with vibesys-cli, so it is tested here.
from vibesys_evaluator._private import environment
from vibesys_evaluator._proto.vibesys.cli.v1 import env_pb

CORPUS = Path(__file__).resolve().parents[4] / "proto/vibesys/cli/v1/conformance/environment.json"
MESSAGES = {
    "JobEnvironment": env_pb.JobEnvironment,
    "KubernetesEnvironment": env_pb.KubernetesEnvironment,
}
CASES = json.loads(CORPUS.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_corpus_is_what_the_codec_does(case: dict) -> None:
    message_type = MESSAGES[case["message"]]
    variables = case["variables"]
    if "error" in case:
        with pytest.raises(environment.EnvironmentValueError) as refused:
            environment.decode_environ(message_type, variables)
        assert list(refused.value.variables) == case["error"]
        for value in variables.values():
            assert len(value) <= 5 or value not in str(refused.value)
        return
    got = environment.decode_environ(message_type, variables)
    want = message_type.from_json(json.dumps(case["decoded"]))
    assert got == want
    assert environment.encode(want) == case.get("writes", variables)


_MOUNT = st.from_regex(r"[a-z0-9][a-z0-9_-]{0,62}", fullmatch=True)
_LOCATION = st.text(st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)), max_size=40).map(
    "/{}".format
)
_JOB = st.builds(
    env_pb.JobEnvironment,
    artifacts=st.one_of(st.just(""), _LOCATION),
    cache=st.one_of(st.just(""), _LOCATION),
    service_url=st.one_of(st.just(""), st.from_regex(r"http://[a-z0-9.:]{1,30}", fullmatch=True)),
    timeout_seconds=st.integers(0, 31536000),
    mounts=st.lists(_MOUNT, max_size=5, unique=True),
)


@given(_JOB)
def test_any_valid_job_environment_round_trips(message: env_pb.JobEnvironment) -> None:
    written = environment.encode(message)
    assert all(written.values())
    assert environment.decode_environ(env_pb.JobEnvironment, written) == message


@given(
    st.sampled_from(
        [
            "VIBESYS_ARTIFACTS_DIR",
            "VIBESYS_CACHE_DIR",
            "VIBESYS_SERVICE_URL",
            "VIBESYS_JOB_TIMEOUT_SECONDS",
            "VIBESYS_MOUNTS",
        ]
    ),
    st.text("abc09 :/,", min_size=1, max_size=12),
)
def test_a_bad_value_is_refused_naming_its_variable(variable: str, tail: str) -> None:
    # Every variable above refuses a value that starts with a capital Q (a relative location, a
    # URL that is not http, not digits, an upper-case mount name).
    value = "QzxqzxQ" + tail
    with pytest.raises(environment.EnvironmentValueError) as refused:
        environment.decode_environ(env_pb.JobEnvironment, {variable: value})
    assert refused.value.variables == (variable,)
    assert value not in str(refused.value)


def test_a_variable_that_is_unset_is_the_zero_value() -> None:
    assert environment.job_environment({}).timeout_seconds == 0
    assert environment.job_environment({"VIBESYS_JOB_TIMEOUT_SECONDS": ""}).timeout_seconds == 0
