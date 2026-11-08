"""The JSONSchemaBench workload: which schemas are asked, what the prompt says, what counts as right.

Everything here is pure except `model_dir` and `load_pool`, which read the Hugging Face cache (and
download once). `bench.py` runs it against the candidate's server.
"""

from __future__ import annotations

import hashlib
import json
import random
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema.validators import validator_for

DATASET_ID = "epfl-dlab/JSONSchemaBench"
DATASET_REVISION = "5bd0f4640badc6f3f02df796421d21cb0ca0b141"
# The val split of the dataset's default config, which holds every subset's val rows.
DATASET_FILE = "data/val-00000-of-00001.parquet"
MODEL_ID = "mlx-community/Meta-Llama-3.1-8B-Instruct-8bit"
MODEL_REVISION = "142d428004044c37c441272c91316251d9aecc58"

# The val split holds 937 schemas up to 6.8 MB of JSON. Schemas above this size are not asked: a
# prompt that carries one takes minutes to prefill on a laptop, and its answer more tokens than the
# budget below.
MAX_SCHEMA_CHARS = 1500
SELECTION_SEED = 42
MAX_TOKENS = 256
# The gate: a commit passes the check when at least this share of the answers validate against
# their schema, and of the answers to schemas that can hold the sentinel, include it. The baseline
# engine (greedy Llama 3.1 8B under llguidance) answers 37 of 40 validly and 30 or 31 of 35 with the
# sentinel; the misses are an object whose properties the model wrote in another order than the
# grammar allows, and answers cut off by MAX_TOKENS. An engine that ignores the schema or the
# prompt answers far fewer, so the gate sits a few answers under the baseline.
MIN_VALID_RATE = 0.85
MIN_SENTINEL_RATE = 0.80

TASK_HEADING = "Generate one JSON value."


@dataclass(frozen=True)
class Case:
    """One request: a schema, the prompt that carries it, and the sentinel the answer must hold."""

    unique_id: str
    schema: dict[str, Any]
    prompt: str
    sentinel: str | None


@dataclass(frozen=True)
class Verdict:
    """What one answer is worth: whether it validates, and whether it holds the sentinel."""

    valid: bool
    error: str | None
    sentinel_ok: bool | None  # None when the schema cannot hold a sentinel


def model_dir() -> Path:
    """The pinned model's snapshot in the Hugging Face cache (downloaded on first use, ~8.5 GB)."""
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(MODEL_ID, revision=MODEL_REVISION))


def load_pool() -> list[tuple[str, dict[str, Any]]]:
    """The schemas that can be asked: `(unique_id, schema)` of the val split, small and well formed."""
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(DATASET_ID, DATASET_FILE, repo_type="dataset", revision=DATASET_REVISION)
    pool = []
    for row in pq.read_table(path).to_pylist():
        if len(row["json_schema"]) > MAX_SCHEMA_CHARS:
            continue
        schema = json.loads(row["json_schema"])
        if isinstance(schema, dict) and is_well_formed(schema):
            pool.append((row["unique_id"], schema))
    return pool


def is_well_formed(schema: dict[str, Any]) -> bool:
    """Whether the schema is itself a valid JSON Schema: no answer validates against one that is not."""
    try:
        validator_for(schema).check_schema(schema)
    except Exception:
        return False
    return True


def select(pool: list[tuple[str, dict[str, Any]]], count: int) -> list[Case]:
    """The first `count` schemas of the pool in a fixed pseudo-random order.

    The order depends only on `SELECTION_SEED` and each schema's id, so a smaller count is a prefix
    of a larger one, and the same on every machine and Python version.
    """

    def rank(item: tuple[str, dict[str, Any]]) -> str:
        return hashlib.sha256(f"{SELECTION_SEED}:{item[0]}".encode()).hexdigest()

    return [case(unique_id, schema) for unique_id, schema in sorted(pool, key=rank)[:count]]


def case(unique_id: str, schema: dict[str, Any]) -> Case:
    """The request for one schema. Its sentinel is fixed by the id, so every run asks the same."""
    sentinel = None
    if can_hold_sentinel(schema):
        rng = random.Random(f"{SELECTION_SEED}:{unique_id}")
        sentinel = "sx-" + "".join(rng.choices(string.ascii_lowercase + string.digits, k=8))
    return Case(unique_id, schema, build_prompt(schema, sentinel), sentinel)


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


def judge(request: Case, text: str) -> Verdict:
    """Whether the answer parses, validates against the request's schema, and holds its sentinel."""
    sentinel_ok = None if request.sentinel is None else request.sentinel in text
    try:
        value = json.loads(text)
    except ValueError as error:
        return Verdict(False, f"parse failed: {error}", sentinel_ok)
    try:
        validator_for(request.schema)(request.schema).validate(value)
    except Exception as error:
        return Verdict(
            False, f"schema validation failed: {str(error).splitlines()[0]}", sentinel_ok
        )
    return Verdict(True, None, sentinel_ok)


def gate(verdicts: list[Verdict]) -> str | None:
    """None when the answers pass the gate, else why not."""
    valid = sum(v.valid for v in verdicts)
    held = [v.sentinel_ok for v in verdicts if v.sentinel_ok is not None]
    valid_rate = valid / len(verdicts)
    sentinel_rate = sum(held) / len(held) if held else 1.0
    if valid_rate < MIN_VALID_RATE:
        return f"schema validity {valid}/{len(verdicts)} is below {MIN_VALID_RATE:.2f}"
    if sentinel_rate < MIN_SENTINEL_RATE:
        return f"sentinel inclusion {sum(held)}/{len(held)} is below {MIN_SENTINEL_RATE:.2f}"
    return None
