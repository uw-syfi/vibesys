"""JSON-schema constrained decoding: llguidance token masks applied to MLX logits.

``Constrainer`` compiles a schema into a matcher per request. ``SchemaMask`` is the mlx-lm logits
processor that keeps the matcher in step with the sampled tokens and masks every token the schema
does not allow, so a finished generation is valid for the schema (when it is not cut off by
``max_tokens``).
"""

from __future__ import annotations

from typing import Any

import mlx.core as mx
from llguidance import LLMatcher, LLTokenizer
from llguidance.hf import from_tokenizer
from llguidance.mlx import apply_token_bitmask
from llguidance.numpy import allocate_token_bitmask, fill_next_token_bitmask

# Compact JSON: no optional whitespace, so a greedy decode cannot spend its token budget on blanks.
# ``coerce_one_of`` reads oneOf as anyOf and ``lenient`` skips the keywords llguidance has no
# grammar for (dependencies, uniqueItems, not, unknown formats); the checker holds the output to the
# whole schema either way.
_JSON_OPTIONS = {"whitespace_flexible": False, "coerce_one_of": True, "lenient": True}


class SchemaError(ValueError):
    """The schema cannot be compiled into a grammar."""


class SchemaMask:
    """An mlx-lm logits processor that allows only the tokens the schema's grammar allows next."""

    def __init__(self, matcher: LLMatcher, vocab_size: int) -> None:
        self._matcher = matcher
        self._bitmask = allocate_token_bitmask(1, vocab_size)
        self._started = False

    def __call__(self, tokens: mx.array, logits: mx.array) -> mx.array:
        # The first call sees the prompt; every later call sees the token sampled last.
        if self._started and not self._matcher.consume_token(int(tokens[-1].item())):
            raise SchemaError(f"grammar rejected a sampled token: {self._matcher.get_error()}")
        self._started = True
        fill_next_token_bitmask(self._matcher, self._bitmask)
        return apply_token_bitmask(logits, self._bitmask)


class Constrainer:
    """Builds a ``SchemaMask`` for a schema, over the tokenizer of the served model."""

    def __init__(self, hf_tokenizer: Any, vocab_size: int, eos_token_ids: set[int]) -> None:
        self._vocab_size = vocab_size
        self._tokenizer: LLTokenizer = from_tokenizer(
            hf_tokenizer, n_vocab=vocab_size, eos_token=sorted(eos_token_ids)
        )

    def mask_for(self, schema: dict[str, Any]) -> SchemaMask:
        """The logits processor for one generation.

        Raises:
            SchemaError: llguidance does not support the schema.
        """
        grammar = LLMatcher.grammar_from_json_schema(schema, defaults=_JSON_OPTIONS)
        matcher = LLMatcher(self._tokenizer, grammar)
        if matcher.is_error():
            raise SchemaError(matcher.get_error())
        return SchemaMask(matcher, self._vocab_size)
