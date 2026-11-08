"""The JSONSchemaBench workload: which schemas are asked, what the prompt says, what counts as right.

Everything here is pure except `model_dir` and `load_pool`, which read the Hugging Face cache (and
download once). `bench.py` runs it against the candidate's server.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

from jsonschema.validators import validator_for

DATASET_ID = "epfl-dlab/JSONSchemaBench"
DATASET_REVISION = "5bd0f4640badc6f3f02df796421d21cb0ca0b141"
MODEL_ID = "mlx-community/Meta-Llama-3.1-8B-Instruct-8bit"
MODEL_REVISION = "142d428004044c37c441272c91316251d9aecc58"


class Split(StrEnum):
    """The dataset's own splits (the default config, which holds every subset's rows).

    The loop is scored on VAL, in the check and the benchmark. TEST is held out: only `bench.py
    holdout` asks it, for a final or promoted commit. TRAIN is the agent's own to test with.
    """

    VAL = "val"
    TEST = "test"
    TRAIN = "train"

    @property
    def file(self) -> str:
        return f"data/{self.value}-00000-of-00001.parquet"


# A split holds schemas up to 6.8 MB of JSON. A schema above this size is not asked, in any split:
# its prompt takes minutes to prefill on a laptop, which the check's and the benchmark's time does
# not allow. (Schemas that are themselves invalid JSON Schema are left out too: `is_well_formed`.)
MAX_SCHEMA_CHARS = 1500
SELECTION_SEED = 42
MAX_TOKENS = 512


class Ranks(NamedTuple):
    """Which schemas of a split a mode asks: `count` of them from rank `start` of the fixed order."""

    start: int
    count: int


# The ranks of VAL each mode asks. They do not overlap, so what the benchmark measures is not what
# the check checked, and the warm-up request is neither. HOLDOUT is of TEST.
BENCHMARK = Ranks(0, 24)
CHECK = Ranks(24, 40)
PROFILE = Ranks(0, 4)
WARM_UP = Ranks(64, 1)
HOLDOUT = Ranks(0, 96)
HOLDOUT_WARM_UP = Ranks(96, 1)

# The gate. The server controls whether an answer that ended is right, so every answer that ended
# must validate against its schema. A server does not control whether the model needs more than
# MAX_TOKENS tokens, so an invalid answer is excused when the judge verifies that it was cut off: it
# counts the answer's tokens itself, with the pinned model's tokenizer, and never reads the server's
# finish_reason (the candidate is untrusted). Verified cut-offs are allowed up to a fraction of the
# answers, and the sentinel must be in at least MIN_SENTINEL_RATE of the answers that ended and
# could hold one.
#
# Measured on the baseline engine (greedy Llama 3.1 8B under llguidance, 512 tokens allowed). In
# process, over the first 48 schemas of at most 6000 characters: 45 answers ended valid, 3 were cut
# off (a model that leaves the order of properties the grammar allows writes garbage until the
# limit, or never closes a long string), none ended invalid. The longest answer that ended on its own
# was 268 tokens by the judge's count; the shortest cut-off was 384: the engine had spent all 512
# tokens, but a constrained decoder emits more, smaller tokens than the tokenizer's own encoding of
# the same text. So an invalid answer of at least TRUNCATION_MIN_TOKENS (above the one, below the
# other) was cut off, and a shorter one ended. Through vibesys-cli, six runs of the baseline (224
# answers: the check and the benchmark, two held-out runs, and the check and the benchmark at a cap
# of 6000 characters) had 4 answers cut off (1.8%); in process, 3 of 48 (6%). Cut-offs are not the same
# schemas from run to run, as the sentinel in the prompt changes them. A
# 24-answer sample with a 5% allowance (one answer) would be aborted by two cut-offs, which at 3%
# per answer happens once in six samples; at 10% (two answers) once in thirty.
TRUNCATION_MIN_TOKENS = 320
MAX_TRUNCATED_FRACTION = 0.10
MIN_SENTINEL_RATE = 0.80

TASK_HEADING = "Generate one JSON value."


@dataclass(frozen=True)
class Case:
    """One request: a schema, the prompt that carries it, and the sentinel the answer must hold."""

    unique_id: str
    schema: dict[str, Any]
    prompt: str
    sentinel: str | None


class Outcome(StrEnum):
    """What an answer is: right, wrong although it ended, or wrong and cut off."""

    VALID = "valid"
    INVALID = "invalid"  # it ended, and is not JSON that validates against the schema
    CUT_OFF = "cut_off"  # invalid, and as long as the token limit lets an answer be (verified)


@dataclass(frozen=True)
class Verdict:
    """What one answer is worth: whether it validates, and whether it holds the sentinel."""

    outcome: Outcome
    error: str | None
    sentinel_ok: bool | None  # None when the schema cannot hold a sentinel

    @property
    def valid(self) -> bool:
        return self.outcome is Outcome.VALID


def model_dir() -> Path:
    """The pinned model's snapshot in the Hugging Face cache (downloaded on first use, ~8.5 GB)."""
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(MODEL_ID, revision=MODEL_REVISION))


def load_pool(split: Split = Split.VAL) -> list[tuple[str, dict[str, Any]]]:
    """The schemas of a split that can be asked: `(unique_id, schema)`, small and well formed."""
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(DATASET_ID, split.file, repo_type="dataset", revision=DATASET_REVISION)
    pool = []
    for row in pq.read_table(path).to_pylist():
        if len(row["json_schema"]) > MAX_SCHEMA_CHARS:
            continue
        schema = json.loads(row["json_schema"])
        if isinstance(schema, dict) and is_well_formed(schema):
            pool.append((row["unique_id"], schema))
    return pool


def is_well_formed(schema: dict[str, Any]) -> bool:
    """Whether the schema is itself a valid JSON Schema: no answer validates against one that is not.

    This is the only reason a schema is left out besides its size. A schema that a decoder cannot
    compile is not defective, so it stays: the decoder's gap is the candidate's failure.
    """
    try:
        validator_for(schema).check_schema(schema)
    except Exception:
        return False
    return True


def new_sentinel() -> str:
    """A token nobody has seen, drawn from the operating system's randomness at run time.

    It is never derived from the schema's id or a fixed seed: with the same prompts on every run, a
    candidate could ship a table of memorized answers.
    """
    return "sx-" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))


def select(
    pool: list[tuple[str, dict[str, Any]]],
    ranks: Ranks,
    new_sentinel: Callable[[], str] = new_sentinel,
) -> list[Case]:
    """The schemas at `ranks` of the pool in a fixed pseudo-random order, each with a fresh sentinel.

    Which schemas depends only on `SELECTION_SEED` and each schema's id: the same on every machine
    and Python version, and the ranks of two modes that do not overlap never share a schema. Only
    the sentinels differ from one run to the next.
    """

    def rank(item: tuple[str, dict[str, Any]]) -> str:
        return hashlib.sha256(f"{SELECTION_SEED}:{item[0]}".encode()).hexdigest()

    chosen = sorted(pool, key=rank)[ranks.start : ranks.start + ranks.count]
    return [case(unique_id, schema, new_sentinel()) for unique_id, schema in chosen]


def case(unique_id: str, schema: dict[str, Any], sentinel: str) -> Case:
    """The request for one schema; it carries the sentinel only if the schema can hold a string."""
    held = sentinel if can_hold_sentinel(schema) else None
    return Case(unique_id, schema, build_prompt(schema, held), held)


_SOURCES = (
    ("github", re.compile(r"o\d+")),
    ("kubernetes", re.compile(r"kb_\d+_Normalized")),
    ("snowplow", re.compile(r"sp_\d+_Normalized")),
    ("washingtonpost", re.compile(r"wp_\d+_Normalized")),
    ("glaive", re.compile(r"[a-z_]+_[0-9a-f]{8}")),
)


def source_of(unique_id: str) -> str:
    """The JSONSchemaBench subset a schema comes from, read off its id (the parquet has no column).

    The ids of a subset share a shape: `o123` (GitHub), `kb_12_Normalized` (Kubernetes),
    `sp_12_Normalized` (Snowplow), `wp_12_Normalized` (Washington Post), `calculate_area_1a2b3c4d`
    (Glaive function calls). A schema with any other id is one of the JSON Schema Store's, which
    are named by hand.
    """
    for name, shape in _SOURCES:
        if shape.fullmatch(unique_id):
            return name
    return "store"


def can_hold_sentinel(schema: Any) -> bool:
    """Whether a value valid for the schema can include an arbitrary string."""
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    if kind == "string" or (isinstance(kind, list) and "string" in kind):
        return True
    if kind == "array":
        return can_hold_sentinel(schema.get("items"))
    if kind == "object" or "properties" in schema:
        if any(can_hold_sentinel(value) for value in (schema.get("properties") or {}).values()):
            return True
        extra = schema.get("additionalProperties")
        return extra is True or (isinstance(extra, dict) and can_hold_sentinel(extra))
    return any(
        can_hold_sentinel(item)
        for key in ("oneOf", "anyOf", "allOf")
        for item in schema.get(key, [])
        if isinstance(item, dict)
    )


def build_prompt(schema: dict[str, Any], sentinel: str | None) -> str:
    """The text of a request: the task, the schema and, when it can hold one, the sentinel."""
    heading = schema.get("description") or schema.get("title") or TASK_HEADING
    ask = ""
    if sentinel is not None:
        ask = f"\nInclude the exact token {sentinel!r} somewhere inside the JSON as a string value."
    return (
        f"Task: {heading}\n\n"
        "Generate one JSON value that satisfies this JSON Schema:\n\n"
        f"{json.dumps(schema, indent=2)}\n"
        f"{ask}\n\n"
        "Write the properties of each object in the order the schema lists them.\n"
        "Respond with JSON only. No prose, no markdown fences."
    )


def request_body(request: Case) -> dict[str, Any]:
    """The JSON body of the streaming completion request for a case."""
    return {
        "prompt": request.prompt,
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "stream": True,
        "response_format": {"type": "json_schema", "json_schema": {"schema": request.schema}},
    }


def judge(request: Case, text: str, tokens: int) -> Verdict:
    """Whether the answer parses, validates against the request's schema, and holds its sentinel.

    `tokens` is the number of tokens the judge counted in `text` with the pinned tokenizer. An
    invalid answer of at least `TRUNCATION_MIN_TOKENS` tokens is a verified cut-off; a shorter one
    ended, whatever the server says about it.
    """
    sentinel_ok = None if request.sentinel is None else request.sentinel in text
    reason = why_invalid(request, text)
    if reason is None:
        return Verdict(Outcome.VALID, None, sentinel_ok)
    cut_off = tokens >= TRUNCATION_MIN_TOKENS
    return Verdict(Outcome.CUT_OFF if cut_off else Outcome.INVALID, reason, sentinel_ok)


def why_invalid(request: Case, text: str) -> str | None:
    """None when the text is JSON that validates against the request's schema, else why not."""
    try:
        value = json.loads(text)
    except ValueError as error:
        return f"parse failed: {error}"
    try:
        validator_for(request.schema)(request.schema).validate(value)
    except Exception as error:
        return f"schema validation failed: {str(error).splitlines()[0]}"
    return None


def gate(verdicts: list[Verdict]) -> str | None:
    """None when the answers pass the gate, else why not."""
    if not verdicts:
        return "there are no answers"
    ended_invalid = sum(v.outcome is Outcome.INVALID for v in verdicts)
    cut_off = sum(v.outcome is Outcome.CUT_OFF for v in verdicts)
    if ended_invalid:
        return (
            f"{ended_invalid} of {len(verdicts)} answers ended but are not valid for their schema"
        )
    if cut_off / len(verdicts) > MAX_TRUNCATED_FRACTION:
        return (
            f"{cut_off} of {len(verdicts)} answers were cut off at the token limit, "
            f"above {MAX_TRUNCATED_FRACTION:.0%}"
        )
    held = [
        v.sentinel_ok for v in verdicts if v.outcome is Outcome.VALID and v.sentinel_ok is not None
    ]
    if held and sum(held) / len(held) < MIN_SENTINEL_RATE:
        return f"sentinel inclusion {sum(held)}/{len(held)} is below {MIN_SENTINEL_RATE:.2f}"
    return None
