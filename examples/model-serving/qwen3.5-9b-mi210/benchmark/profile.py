#!/usr/bin/env python3
"""Fixed diagnostic load, independent of benchmark preflight and warmup gates."""

from __future__ import annotations

import argparse
import json
import urllib.request
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

MODEL = "Qwen/Qwen3.5-9B"
PROMPTS = (
    "Write a Python function that returns the sum of two integers.",
    "Explain how a FIFO queue processes requests in one sentence.",
)
OUTPUT_TOKENS = 16
REQUEST_TIMEOUT_SECONDS = 60


class ProfileCompletionRequest(BaseModel):
    """The bundle-owned fixed decode request, with no agent-selected knobs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: Literal["Qwen/Qwen3.5-9B"] = MODEL
    prompt: str
    max_tokens: Literal[16] = OUTPUT_TOKENS
    temperature: Literal[0] = 0
    ignore_eos: Literal[True] = True


class CompletionChoice(BaseModel):
    """Completion text; optional OpenAI response extensions are not consumed."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    text: str


class CompletionUsage(BaseModel):
    """Decoded-token evidence, allowing other OpenAI usage statistics."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    completion_tokens: int = Field(gt=0)


class ProfileCompletionResponse(BaseModel):
    """Serving evidence; unrelated OpenAI response metadata is not consumed."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    choices: tuple[CompletionChoice, ...] = Field(min_length=1)
    usage: CompletionUsage


class CompletionClient(Protocol):
    """Send one fixed completion request and return validated serving evidence."""

    def complete(self, request: ProfileCompletionRequest) -> ProfileCompletionResponse: ...


class HttpCompletionClient:
    """Direct HTTP transport; no session runner or cache telemetry required."""

    def __init__(self, base_url: str) -> None:
        self._url = base_url.rstrip("/") + "/completions"

    def complete(self, request: ProfileCompletionRequest) -> ProfileCompletionResponse:
        http_request = urllib.request.Request(
            self._url,
            data=request.model_dump_json().encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(http_request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return ProfileCompletionResponse.model_validate_json(response.read())


def run_profile(client: CompletionClient) -> int:
    """Exercise prefill and decode twice, rejecting a server that never serves.

    EOS is ignored for this fixed decode load. This run establishes serving
    evidence, not accuracy or throughput acceptance. Empty decoded text is
    valid when the generated tokens are special tokens.
    """
    for prompt in PROMPTS:
        client.complete(ProfileCompletionRequest(prompt=prompt))
    return len(PROMPTS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args(argv)
    completed = run_profile(HttpCompletionClient(args.base_url))
    print(json.dumps({"profile_requests_completed": completed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
