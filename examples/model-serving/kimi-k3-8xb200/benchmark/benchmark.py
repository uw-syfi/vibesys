#!/usr/bin/env python3
"""Prepare Kimi-K3's tokenizer, then delegate to the shared RF text driver."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_MODEL = "moonshotai/Kimi-K3"
_TOKENIZER_REVISION = "f831ab66814297da540d832a5235f8e904f29d06"
_TOKENIZER_BASE_SIZE = 163_584
_TOKENIZER_RESERVED_SIZE = 256
_TOKENIZER_PATTERN = "|".join(
    (
        r"[\p{Han}]+",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]*"
        r"[\p{Ll}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]+"
        r"[\p{Ll}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    )
)
_NAMED_TOKENS = {
    163_584: "[BOS]",
    163_585: "[EOS]",
    163_586: "<|end_of_msg|>",
    163_587: "<|open|>",
    163_588: "<|close|>",
    163_589: "<|sep|>",
    163_590: "[start_header_id]",
    163_591: "[end_header_id]",
    163_593: "[EOT]",
    163_602: "<|media_begin|>",
    163_603: "<|media_content|>",
    163_604: "<|media_end|>",
    163_605: "<|media_pad|>",
    163_649: "<osagent_mode>",
    163_838: "[UNK]",
    163_839: "[PAD]",
}
_FIXED_TEXT_DRIVER_ENV = "VIBESYS_REQUEST_FACTORY_FIXED_TEXT_DRIVER"
_REQUESTS = 256
_INPUT_TOKENS = 4096
_OUTPUT_TOKENS = 2048
_CONCURRENCY = 32


def _prepare_kimi_tokenizer(directory: Path) -> Path:
    """Convert Kimi's pinned tiktoken vocabulary to RF's tokenizer.json format."""
    from huggingface_hub import hf_hub_download
    from tiktoken.load import load_tiktoken_bpe
    from transformers.convert_slow_tokenizer import TikTokenConverter

    vocab_file = hf_hub_download(
        repo_id=_MODEL,
        filename="tiktoken.model",
        revision=_TOKENIZER_REVISION,
    )
    mergeable_ranks = load_tiktoken_bpe(vocab_file)
    if len(mergeable_ranks) != _TOKENIZER_BASE_SIZE:
        raise ValueError(
            f"Kimi tokenizer has {len(mergeable_ranks)} base tokens, "
            f"expected {_TOKENIZER_BASE_SIZE} at revision {_TOKENIZER_REVISION}"
        )
    special_tokens = [
        _NAMED_TOKENS.get(token_id, f"<|reserved_token_{token_id}|>")
        for token_id in range(
            _TOKENIZER_BASE_SIZE,
            _TOKENIZER_BASE_SIZE + _TOKENIZER_RESERVED_SIZE,
        )
    ]
    tokenizer = TikTokenConverter(
        vocab_file,
        _TOKENIZER_PATTERN,
        False,
        special_tokens,
    ).converted()
    tokenizer_path = directory / "tokenizer.json"
    tokenizer.save(str(tokenizer_path))
    return tokenizer_path


def run(args: argparse.Namespace) -> int:
    """Keep tokenizer resources alive while the shared driver runs."""
    driver = os.environ.get(_FIXED_TEXT_DRIVER_ENV)
    if driver is None or not Path(driver).is_file():
        raise RuntimeError(
            f"{_FIXED_TEXT_DRIVER_ENV} must name the installed fixed-text driver; "
            "run this benchmark through request-factory-adapter"
        )

    with tempfile.TemporaryDirectory(prefix="vibesys-rf-kimi-") as directory:
        tokenizer_path = (
            Path(args.tokenizer) if args.tokenizer else _prepare_kimi_tokenizer(Path(directory))
        )
        command = [
            sys.executable,
            driver,
            "--request-factory-engine",
            args.request_factory_engine,
            "--url",
            args.url,
            "--model",
            args.model,
            "--tokenizer",
            str(tokenizer_path),
            "--request-count",
            str(args.request_count),
            "--input-tokens",
            str(args.input_tokens),
            "--output-tokens",
            str(args.output_tokens),
            "--concurrency",
            str(args.concurrency),
        ]
        if args.vs_output:
            command.extend(("--vs-output", args.vs_output))
        # lint-waiver: LW-031732 [S603]; the evaluator exports the installed driver path,
        # > and every workload value is forwarded as an argv element without a shell.
        completed = subprocess.run(command, check=False)  # noqa: S603
        return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", required=True)
    parser.add_argument("--vs-output")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--model", default=_MODEL)
    parser.add_argument(
        "--tokenizer",
        help=(
            "existing tokenizer.json path; by default convert Kimi-K3's tiktoken.model "
            f"at pinned revision {_TOKENIZER_REVISION}"
        ),
    )
    parser.add_argument("--request-count", type=int, default=_REQUESTS)
    parser.add_argument("--input-tokens", type=int, default=_INPUT_TOKENS)
    parser.add_argument("--output-tokens", type=int, default=_OUTPUT_TOKENS)
    parser.add_argument("--concurrency", type=int, default=_CONCURRENCY)
    args = parser.parse_args()
    if min(args.request_count, args.input_tokens, args.output_tokens, args.concurrency) <= 0:
        parser.error("request count, token lengths, and concurrency must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
