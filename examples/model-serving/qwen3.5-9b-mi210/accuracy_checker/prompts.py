"""Fixed prompt set for the accuracy gate.
Covers short raw completions, chat-templated prompts (thinking disabled), code,
arithmetic, structured output, and long prompts whose lengths straddle the
64-token GDN chunk boundary and exercise RoPE at larger positions. Prompt token
ids are frozen into `golden.json`, so tokenizer drift cannot move the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PromptCase:
    name: str
    kind: Literal["raw", "chat"]
    text: str
    max_new_tokens: int = 64


def _ledger(n: int) -> str:
    """Deterministic long context: n distinct records, then a retrieval question."""
    cities = [
        "Oslo",
        "Lima",
        "Hanoi",
        "Accra",
        "Perth",
        "Quito",
        "Riga",
        "Dakar",
        "Kyoto",
        "Tunis",
        "Porto",
    ]
    goods = [
        "copper wire",
        "rice",
        "ceramic tiles",
        "wool",
        "solar panels",
        "coffee",
        "timber",
        "glass jars",
    ]
    lines = [
        f"Record {i}: the depot in {cities[(i * 7) % len(cities)]} shipped {(i * 37) % 900 + 100} crates of "
        f"{goods[(i * 5) % len(goods)]} on day {(i * 11) % 365 + 1}, handled by clerk #{(i * 13) % 97}."
        for i in range(n)
    ]
    q = n // 3
    return (
        "Shipping ledger.\n"
        + "\n".join(lines)
        + f"\n\nQuestion: which depot shipped the goods in Record {q}, and how many crates? Answer:"
    )


CASES: tuple[PromptCase, ...] = (
    PromptCase("fact_raw", "raw", "The capital of France is"),
    PromptCase(
        "story_raw",
        "raw",
        "Once upon a time, in a small village at the edge of a dark forest, there lived",
    ),
    PromptCase("code_raw", "raw", 'def quicksort(arr):\n    """Sort a list using quicksort."""\n'),
    PromptCase(
        "json_raw", "raw", 'Here is a JSON object describing a book:\n{"title": "Dune", "author":'
    ),
    PromptCase("numbers_raw", "raw", "1, 1, 2, 3, 5, 8, 13, 21,"),
    PromptCase("single_word", "raw", "Photosynthesis"),
    PromptCase("chat_explain", "chat", "Explain in three sentences why the sky is blue."),
    PromptCase(
        "chat_math",
        "chat",
        "A train travels 120 km in 1.5 hours. What is its average speed in km/h? Show brief reasoning.",
    ),
    PromptCase(
        "chat_code",
        "chat",
        "Write a Python function that checks whether a string is a palindrome, ignoring case and spaces.",
    ),
    PromptCase(
        "chat_translate",
        "chat",
        "Translate to German and French: 'The meeting has been moved to Thursday afternoon.'",
    ),
    PromptCase("ledger_mid", "raw", _ledger(40)),  # ~1.2k tokens
    PromptCase("ledger_long", "chat", _ledger(130)),  # ~4k tokens, many GDN chunks
)
