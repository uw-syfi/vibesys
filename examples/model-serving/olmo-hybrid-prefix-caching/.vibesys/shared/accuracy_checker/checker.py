"""Accuracy checker: the running server's greedy completions must equal the HF reference.

The reference is ``transformers`` ``AutoModelForCausalLM.generate`` (bfloat16, eager
attention, greedy) on ten fixed base-model prompts, stored in ``reference_outputs.json``
beside this file; ``regenerate_reference.py`` produced it, and ``README.md`` gives its
provenance. For each prompt the checker sends the reference prompt token IDs to
``<url>/v1/completions`` as ``prompt: list[int]``, non-streaming, with the prompt's
``max_tokens``, ``temperature: 0`` and EOS honored (no ``ignore_eos``).
``choices[0].text`` must equal that prompt's reference text exactly; where the
plain-torch and flash-linear-attention references differ, either text passes. Every
prompt must pass.

The checker uses only the standard library, runs on Python 3.9 or newer, and talks to
the server over HTTP, so it runs next to the server (for example inside a Modal serving
container) without loading a second copy of the model.

Usage:
    python checker.py                      # server on http://localhost:8000
    python checker.py --url http://host:8000 --model allenai/Olmo-Hybrid-7B

Public API: ``Case``, ``Reference``, ``load_reference``, ``CompletionRequest``,
``completion_request``, ``completion_text``, the outcomes ``Passed``, ``Mismatched`` and
``Failed``, ``check``, ``report`` and ``main``. Only ``main`` does I/O of its own; ``check``
reaches the server through the ``send`` callable it is given, and ``report`` writes to
the stream it is given.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

DEFAULT_URL = "http://localhost:8000"
DEFAULT_MODEL = "allenai/Olmo-Hybrid-7B"
ENDPOINT = "/v1/completions"
REFERENCE_PATH = Path(__file__).resolve().with_name("reference_outputs.json")


@dataclass(frozen=True)
class Case:
    """One prompt and each variant's reference text, any of which a correct server returns."""

    description: str
    prompt: str
    prompt_token_ids: tuple[int, ...]
    max_tokens: int
    references: Mapping[str, str]


@dataclass(frozen=True)
class Reference:
    """The validated contents of ``reference_outputs.json``."""

    model: str
    revision: str
    variants: Mapping[str, Mapping[str, object]]
    cases: tuple[Case, ...]


def _fields(value: object, keys: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a JSON object")
    if set(value) != keys:
        unknown = sorted(set(value) - keys)
        missing = sorted(keys - set(value))
        raise ValueError(f"{where}: unknown keys {unknown}, missing keys {missing}")
    return value


def _token_ids(value: object, where: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(type(token) is not int for token in value):
        raise ValueError(f"{where} must be a list of integers")
    return tuple(value)


def _case(raw: object, variants: set[str], where: str) -> Case:
    row = _fields(
        raw, {"description", "prompt", "max_tokens", "prompt_token_ids", "outputs"}, where
    )
    for key in ("description", "prompt"):
        if not isinstance(row[key], str):
            raise ValueError(f"{where}.{key} must be a string")
    max_tokens = row["max_tokens"]
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError(f"{where}.max_tokens must be a positive integer")
    prompt_ids = _token_ids(row["prompt_token_ids"], f"{where}.prompt_token_ids")
    if not prompt_ids:
        raise ValueError(f"{where}.prompt_token_ids must not be empty")
    references = {}
    for variant, output in _fields(row["outputs"], variants, f"{where}.outputs").items():
        at = f"{where}.outputs.{variant}"
        output = _fields(output, {"token_ids", "text"}, at)
        if len(_token_ids(output["token_ids"], f"{at}.token_ids")) > max_tokens:
            raise ValueError(f"{at}.token_ids is longer than max_tokens")
        if not isinstance(output["text"], str):
            raise ValueError(f"{at}.text must be a string")
        references[variant] = output["text"]
    return Case(row["description"], row["prompt"], prompt_ids, max_tokens, references)


def load_reference(document: object) -> Reference:
    """Validate a parsed reference file; raise ``ValueError`` naming the offending key."""
    top = _fields(document, {"model", "generation", "variants", "cases"}, "reference")
    model = _fields(top["model"], {"id", "revision"}, "model")
    if not all(isinstance(model[key], str) and model[key] for key in ("id", "revision")):
        raise ValueError("model.id and model.revision must be non-empty strings")
    variants = top["variants"]
    if not isinstance(variants, dict) or not variants:
        raise ValueError("variants must be a non-empty JSON object")
    for name, environment in variants.items():
        if not isinstance(environment, dict):
            raise ValueError(f"variants.{name} must be a JSON object")
    if not isinstance(top["cases"], list) or not top["cases"]:
        raise ValueError("cases must be a non-empty list")
    cases = tuple(
        _case(raw, set(variants), f"cases[{index}]") for index, raw in enumerate(top["cases"])
    )
    return Reference(model["id"], model["revision"], variants, cases)


@dataclass(frozen=True)
class CompletionRequest:
    """One ``POST`` of a JSON body to a URL."""

    url: str
    body: Mapping[str, object]


def completion_request(base_url: str, model: str, case: Case) -> CompletionRequest:
    """The greedy, non-streaming completions request for ``case``; EOS is honored."""
    return CompletionRequest(
        url=base_url.rstrip("/") + ENDPOINT,
        body={
            "model": model,
            "prompt": list(case.prompt_token_ids),
            "max_tokens": case.max_tokens,
            "temperature": 0.0,
            "stream": False,
        },
    )


def completion_text(payload: object) -> str:
    """The text of the one choice in a completions response; ``ValueError`` otherwise."""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("the response must hold exactly one choice in `choices`")
    text = choices[0].get("text")
    if not isinstance(text, str):
        raise ValueError("choices[0].text must be a string")
    return text


@dataclass(frozen=True)
class Passed:
    """The server returned the reference text of ``variants``."""

    case: Case
    text: str
    variants: tuple[str, ...]


@dataclass(frozen=True)
class Mismatched:
    """The server returned a text that no reference variant has."""

    case: Case
    text: str


@dataclass(frozen=True)
class Failed:
    """The request failed, or the response was not a completion."""

    case: Case
    error: str


def check(
    cases: Sequence[Case],
    base_url: str,
    model: str,
    send: Callable[[CompletionRequest], object],
) -> list[Passed | Mismatched | Failed]:
    """Send every case to the server and judge the returned text against the references.

    ``send`` POSTs a request and returns the parsed JSON response. It raises ``OSError``
    when the request fails (connection, timeout, HTTP error status) and ``ValueError``
    when the body is not JSON; either fails that case, and the remaining cases still run.
    """
    outcomes: list[Passed | Mismatched | Failed] = []
    for case in cases:
        try:
            text = completion_text(send(completion_request(base_url, model, case)))
        except (OSError, ValueError) as exc:
            outcomes.append(Failed(case, f"{type(exc).__name__}: {exc}"))
            continue
        matched = tuple(name for name, ref in case.references.items() if ref == text)
        outcomes.append(Passed(case, text, matched) if matched else Mismatched(case, text))
    return outcomes


def _first_difference(reference: str, text: str) -> int:
    shorter = min(len(reference), len(text))
    return next((i for i in range(shorter) if reference[i] != text[i]), shorter)


def report(
    reference: Reference,
    base_url: str,
    outcomes: Sequence[Passed | Mismatched | Failed],
    out: TextIO,
) -> int:
    """Write the per-case results and a summary; return the exit code, 0 only if all pass."""
    write = out.write
    variants = "; ".join(
        f"{name}: " + ", ".join(f"{key}={value}" for key, value in environment.items())
        for name, environment in reference.variants.items()
    )
    write(f"Reference: {reference.model}@{reference.revision} ({variants})\n")
    write("=" * 70 + "\n")
    write(
        f"Running {len(outcomes)} test cases (greedy decoding) against "
        f"{base_url.rstrip('/')}{ENDPOINT}\n"
    )
    write("=" * 70 + "\n")
    for number, outcome in enumerate(outcomes, 1):
        case = outcome.case
        preview = case.prompt[:80].replace("\n", "\\n") + ("..." if len(case.prompt) > 80 else "")
        write(f"\n[{number}/{len(outcomes)}] {case.description}  (max_tokens={case.max_tokens})\n")
        write(f"  Prompt: {preview!r}\n")
        if isinstance(outcome, Passed):
            write(f"  PASS - EXACT match ({', '.join(outcome.variants)}): {outcome.text!r}\n")
        elif isinstance(outcome, Mismatched):
            by_text: dict[str, list[str]] = {}
            for name, ref in case.references.items():
                by_text.setdefault(ref, []).append(name)
            first = min(_first_difference(ref, outcome.text) for ref in by_text)
            write(f"  FAIL - MISMATCH at character {first}.\n")
            for ref, names in by_text.items():
                write(f"  Reference ({', '.join(names)}): {ref!r}\n")
            write(f"  Server: {outcome.text!r}\n")
        else:
            write(f"  FAIL - request failed: {outcome.error}\n")
    passed = sum(isinstance(outcome, Passed) for outcome in outcomes)
    failed = len(outcomes) - passed
    write("\n" + "=" * 70 + "\n")
    write(f"Results: {passed}/{len(outcomes)} passed, {failed}/{len(outcomes)} failed\n")
    write("=" * 70 + "\n")
    if failed or not outcomes:
        write("\nACCURACY CHECK FAILED\n")
        return 1
    write("\nALL CHECKS PASSED\n")
    return 0


def _http_send(timeout: float) -> Callable[[CompletionRequest], object]:
    def send(request: CompletionRequest) -> object:
        http_request = urllib.request.Request(
            request.url,
            data=json.dumps(request.body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode(errors="replace")
            raise OSError(f"HTTP {exc.code} from {request.url}: {detail}") from exc
        except http.client.HTTPException as exc:
            raise OSError(f"{type(exc).__name__} from {request.url}: {exc}") from exc
        return json.loads(body)

    return send


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Accuracy checker: server vs HF reference")
    parser.add_argument("--url", default=DEFAULT_URL, help="server base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="`model` field of each request")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per request")
    args = parser.parse_args(argv)
    if not args.timeout > 0:
        parser.error("--timeout must be positive")
    reference = load_reference(json.loads(REFERENCE_PATH.read_text()))
    outcomes = check(reference.cases, args.url, args.model, _http_send(args.timeout))
    return report(reference, args.url, outcomes, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
