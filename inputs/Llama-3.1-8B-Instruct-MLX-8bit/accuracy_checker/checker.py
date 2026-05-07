"""HTTP accuracy checker for JSONSchemaBench schema generation."""

from __future__ import annotations

import argparse
import json
import random
import string
import sys
from pathlib import Path
from typing import Any

import httpx
from jsonschema.validators import validator_for


BUNDLE_DIR = Path(__file__).resolve().parents[1]
DATASET_ID = "epfl-dlab/JSONSchemaBench"
DATASET_REVISION = "5bd0f4640badc6f3f02df796421d21cb0ca0b141"
SYSTEM_MESSAGE = (
    "You output strict JSON only that conforms to the user's JSON schema. "
    "No prose, no markdown fences, no explanation. Include any exact sentinel "
    "token the user asks for in a suitable string field."
)


def load_cases(
    subset: str,
    split: str,
    limit: int | None,
    seed: int | None,
    revision: str,
    cache_dir: Path | None,
) -> list[dict[str, Any]]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit("The `datasets` package is required to load JSONSchemaBench.") from exc

    ds = load_dataset(
        DATASET_ID,
        subset,
        split=split,
        revision=revision,
        cache_dir=str(cache_dir) if cache_dir is not None else None,
    )
    rng = random.Random(seed)
    indices = list(range(len(ds)))
    if limit is not None and limit < len(indices):
        indices = rng.sample(indices, k=limit)

    cases = []
    for idx in indices:
        row = ds[idx]
        raw_schema = row.get("json_schema") or row.get("schema") or row.get("content")
        if raw_schema is None:
            continue
        schema = json.loads(raw_schema) if isinstance(raw_schema, str) else raw_schema
        cases.append(
            {
                "unique_id": str(row.get("unique_id") or row.get("id") or idx),
                "description": row.get("description") or row.get("title") or schema.get("description") or "",
                "schema": schema,
            }
        )
    if not cases:
        raise SystemExit(f"No schema cases found in {DATASET_ID}:{subset} split={split}")
    return cases


def schema_can_hold_sentinel(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    typ = schema.get("type")
    if typ == "string" or (isinstance(typ, list) and "string" in typ):
        return True
    if typ == "array":
        return schema_can_hold_sentinel(schema.get("items"))
    if typ == "object" or "properties" in schema:
        for value in (schema.get("properties") or {}).values():
            if schema_can_hold_sentinel(value):
                return True
        additional = schema.get("additionalProperties")
        return additional is True or (
            isinstance(additional, dict) and schema_can_hold_sentinel(additional)
        )
    for key in ("oneOf", "anyOf", "allOf"):
        if any(schema_can_hold_sentinel(item) for item in schema.get(key, []) if isinstance(item, dict)):
            return True
    return False


def make_sentinel(rng: random.Random) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "sx-" + "".join(rng.choices(alphabet, k=8))


def build_prompt(schema: dict, description: str, sentinel: str | None) -> str:
    pretty = json.dumps(schema, indent=2, sort_keys=True)
    sentinel_text = ""
    if sentinel is not None:
        sentinel_text = (
            f"\nInclude the exact token {sentinel!r} somewhere inside the JSON "
            "as a string value."
        )
    return (
        f"Task: {description or 'Generate one JSON value.'}\n\n"
        "Generate one JSON value that satisfies this JSON Schema:\n\n"
        f"{pretty}\n"
        f"{sentinel_text}\n\n"
        "Respond with JSON only."
    )


def extract_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    choice = choices[0]
    if "text" in choice:
        return choice.get("text") or ""
    delta = choice.get("delta")
    if isinstance(delta, dict):
        return delta.get("content") or delta.get("text") or ""
    message = choice.get("message")
    if isinstance(message, dict):
        return message.get("content") or ""
    return ""


def request_json(
    client: httpx.Client,
    url: str,
    prompt: str,
    schema: dict,
    max_tokens: int,
    timeout: float,
) -> str:
    body = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"schema": schema},
        },
    }
    pieces: list[str] = []
    with client.stream("POST", url, json=body, timeout=timeout) as response:
        response.raise_for_status()
        if "text/event-stream" not in response.headers.get("content-type", ""):
            return extract_text(json.loads(response.read()))
        for raw_line in response.iter_lines():
            if not raw_line.startswith("data: "):
                continue
            line = raw_line[len("data: ") :].strip()
            if line == "[DONE]":
                break
            text = extract_text(json.loads(line))
            if text:
                pieces.append(text)
    return "".join(pieces)


def validate_output(text: str, schema: dict) -> tuple[bool, str | None]:
    try:
        value = json.loads(text)
    except Exception as exc:
        return False, f"parse failed: {exc}"
    try:
        cls = validator_for(schema)
        cls.check_schema(schema)
        cls(schema).validate(value)
    except Exception as exc:
        return False, f"schema validation failed: {exc}"
    return True, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Check JSON-schema output from an MLX 8-bit Llama server.")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--endpoint", default="/v1/completions")
    parser.add_argument("--dataset-subset", default="full")
    parser.add_argument("--split", default="val")
    parser.add_argument("--dataset-revision", default=DATASET_REVISION)
    parser.add_argument("--dataset-cache-dir", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--min-valid-rate", type=float, default=0.95)
    parser.add_argument("--min-sentinel-rate", type=float, default=0.90)
    args = parser.parse_args()

    cases = load_cases(
        args.dataset_subset,
        args.split,
        args.limit,
        args.seed,
        args.dataset_revision,
        args.dataset_cache_dir,
    )
    rng = random.Random(args.seed)
    url = args.url.rstrip("/") + args.endpoint
    valid = 0
    sentinel_checked = 0
    sentinel_ok = 0
    failures: list[str] = []

    with httpx.Client() as client:
        for idx, case in enumerate(cases, 1):
            schema = case["schema"]
            sentinel = make_sentinel(rng) if schema_can_hold_sentinel(schema) else None
            prompt = build_prompt(schema, case.get("description", ""), sentinel)
            try:
                text = request_json(client, url, prompt, schema, args.max_tokens, args.timeout)
            except Exception as exc:
                failures.append(f"[{idx}] request failed: {exc}")
                continue
            ok, error = validate_output(text, schema)
            if ok:
                valid += 1
            else:
                failures.append(f"[{idx}] {case.get('unique_id')}: {error}; output={text[:200]!r}")
            if sentinel is not None:
                sentinel_checked += 1
                if sentinel in text:
                    sentinel_ok += 1
                else:
                    failures.append(f"[{idx}] sentinel missing: {sentinel!r}")

    valid_rate = valid / len(cases)
    sentinel_rate = sentinel_ok / sentinel_checked if sentinel_checked else 1.0
    passed = valid_rate >= args.min_valid_rate and sentinel_rate >= args.min_sentinel_rate and not any(
        failure.startswith("[") and "request failed" in failure for failure in failures
    )

    print(
        f"schema_valid={valid}/{len(cases)} ({valid_rate:.3f}) "
        f"sentinel={sentinel_ok}/{sentinel_checked} ({sentinel_rate:.3f})"
    )
    if not passed:
        print("FAIL: JSON-schema accuracy gate failed", file=sys.stderr)
        for failure in failures[:20]:
            print(f"- {failure}", file=sys.stderr)
        raise SystemExit(1)
    print("PASS: JSON-schema accuracy gate passed")


if __name__ == "__main__":
    main()
