"""Model execution: mlx-lm generation with an optional JSON-schema constraint."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

from mlx_lm import load, stream_generate
from mlx_lm.sample_utils import make_sampler

from engine.constraint import Constrainer, SchemaMask

# What the model is told before the request: the answer is JSON, and a requested token goes in a string.
SYSTEM_MESSAGE = (
    "You output strict JSON only that conforms to the user's JSON schema. "
    "No prose, no markdown fences, no explanation. Include any exact sentinel "
    "token the user asks for in a suitable string field."
)


class Piece(NamedTuple):
    """A non-empty piece of the completion; ``finish_reason`` is set on the last one only."""

    text: str
    finish_reason: str | None


class Engine:
    """One loaded model. The caller serializes calls: the GPU runs one generation at a time."""

    def __init__(self, model_dir: Path) -> None:
        self._model, self._tokenizer = load(str(model_dir))
        self._constrainer = Constrainer(
            self._tokenizer._tokenizer,
            int(self._model.args.vocab_size),
            set(self._tokenizer.eos_token_ids),
        )

    def stream(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        schema: dict[str, Any] | None,
    ) -> Iterator[Piece]:
        """The completion, in non-empty pieces; the last has the finish reason ("stop" or "length").

        The prompt is the user turn of the model's chat template, after a system message: the model
        is an instruct model and the request is an instruction.

        Raises:
            SchemaError: llguidance supports no grammar for the schema (nothing was generated).
        """
        mask = self._constrainer.mask_for(schema) if schema is not None else None
        return self._pieces(prompt, max_tokens, temperature, mask)

    def _pieces(
        self, prompt: str, max_tokens: int, temperature: float, mask: SchemaMask | None
    ) -> Iterator[Piece]:
        text = self._tokenizer.apply_chat_template(
            [{"role": "system", "content": SYSTEM_MESSAGE}, {"role": "user", "content": prompt}],
            add_generation_prompt=True,
            tokenize=False,
        )
        steps = stream_generate(
            self._model,
            self._tokenizer,
            self._tokenizer.encode(text, add_special_tokens=False),
            max_tokens=max_tokens,
            sampler=make_sampler(temp=temperature),
            logits_processors=[mask] if mask is not None else None,
        )
        # The finish reason arrives with the last step, which may carry no text: hold one piece back.
        held: Piece | None = None
        for step in steps:
            if step.text:
                if held is not None:
                    yield held
                held = Piece(step.text, None)
            if step.finish_reason is not None and held is not None:
                held = held._replace(finish_reason=step.finish_reason)
        if held is not None:
            yield held
