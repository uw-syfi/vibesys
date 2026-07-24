from __future__ import annotations

import argparse
import asyncio
import json
import random
import string
from typing import Any

import httpx
from jsonschema.validators import validator_for

PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sentinel": {"type": "string"},
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0, "maximum": 120},
        "city": {"type": "string"},
        "interests": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 4,
        },
        "active": {"type": "boolean"},
    },
    "required": ["sentinel", "name", "age", "city", "interests", "active"],
    "additionalProperties": False,
}


def make_sentinel(rng: random.Random) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "VS" + "".join(rng.choice(alphabet) for _ in range(10))


def validate_output(text: str, sentinel: str) -> tuple[bool, str | None]:
    try:
        value = json.loads(text)
    except Exception as exc:
        return False, f"JSON parse failed: {exc}"
    try:
        cls = validator_for(PROFILE_SCHEMA)
        cls.check_schema(PROFILE_SCHEMA)
        cls(PROFILE_SCHEMA).validate(value)
    except Exception as exc:
        return False, f"schema validation failed: {exc}"
    if value.get("sentinel") != sentinel:
        return False, f"sentinel mismatch: expected {sentinel!r}, got {value.get('sentinel')!r}"
    return True, None


async def complete_json(
    client: httpx.AsyncClient,
    url: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
) -> tuple[str, str | None]:
    body = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "guided_json": PROFILE_SCHEMA,
    }
    pieces: list[str] = []
    try:
        async with client.stream("POST", url, json=body, timeout=timeout) as response:
            response.raise_for_status()
            async for raw_line in response.aiter_lines():
                if not raw_line.startswith("data: "):
                    continue
                payload = raw_line[len("data: ") :].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                text = (chunk.get("choices") or [{}])[0].get("text") or ""
                if text:
                    pieces.append(text)
    except Exception as exc:  # noqa: BLE001
        return "".join(pieces), f"{type(exc).__name__}: {exc}"
    return "".join(pieces), None


async def run(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    url = args.url.rstrip("/") + args.endpoint
    results = []
    async with httpx.AsyncClient() as client:
        for index in range(args.num_cases):
            sentinel = make_sentinel(rng)
            role = rng.choice(["software engineer", "teacher", "chef", "nurse", "musician"])
            prompt = (
                f"Return only valid JSON for a fictional {role}. "
                f"The JSON must include sentinel exactly as {sentinel!r} in the sentinel field."
            )
            text, error = await complete_json(client, url, prompt, args.max_tokens, args.timeout)
            ok, validation_error = (
                validate_output(text, sentinel) if error is None else (False, error)
            )
            results.append(
                {
                    "case": index,
                    "sentinel": sentinel,
                    "ok": ok,
                    "error": validation_error,
                    "output": text[:500],
                }
            )

    passed = sum(1 for result in results if result["ok"])
    print(json.dumps({"passed": passed, "total": len(results), "results": results}, indent=2))
    return 0 if passed == len(results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate constrained JSON vLLM responses.")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--endpoint", default="/v1/completions")
    parser.add_argument("--num-cases", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=96)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
