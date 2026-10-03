"""Generate agent replies from the output schema a turn declares.

The generator knows no roles: it reads the JSON schema of the turn's Pydantic
response class, so a new role or field is covered without code. Strings are
drawn from a vocabulary that includes the identifiers the prompt mentions (in
backticks), so a generated reply reuses real ids as often as it invents new
ones: the plausible-but-wrong mistakes agents make (a reused id, an id that
does not exist, a parent that is not a candidate).
"""

from __future__ import annotations

import re
import string
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    import random

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]

_MENTION = re.compile(r"`([^`\n]{1,64})`")
_ATTEMPTS = 24
# The share of generated strings that reuse an identifier the prompt mentions.
_REUSE_SHARE = 0.6


def prompt_vocabulary(prompt: str) -> tuple[str, ...]:
    """Return the backticked identifiers of ``prompt``, in first-mention order."""
    return tuple(dict.fromkeys(_MENTION.findall(prompt)))


class ReplyGenerator:
    """Draw JSON values for one schema from a seeded generator.

    A plain generator answers the way a careful agent does: required fields
    only, defaults kept, short arrays, fresh strings. A ``bold`` one fills
    optional fields, reuses the prompt's identifiers, and draws longer arrays,
    the way a careless agent does.
    """

    def __init__(
        self, rng: random.Random, vocabulary: tuple[str, ...] = (), *, bold: bool = False
    ) -> None:
        """Use ``rng`` for every choice and ``vocabulary`` as candidate strings."""
        self._rng = rng
        self._vocabulary = vocabulary
        self._bold = bold

    def valid(self, response_cls: type[BaseModel]) -> BaseModel | None:
        """Return a schema-valid instance, or ``None`` if none was found in a few draws.

        Validators beyond the JSON schema (patterns, cross-field rules) can
        reject a draw; the generator redraws rather than learning them.
        """
        schema = response_cls.model_json_schema()
        for _ in range(_ATTEMPTS):
            try:
                return response_cls.model_validate(self.value(schema, schema))
            except ValidationError:
                continue
        return None

    def invalid(self, response_cls: type[BaseModel]) -> Json:
        """Return JSON that the declared schema rejects (a wrong type or a missing field)."""
        schema = response_cls.model_json_schema()
        payload = self.value(schema, schema)
        if not isinstance(payload, dict) or not payload:
            return [payload]
        key = self._rng.choice(sorted(payload))
        if self._rng.getrandbits(1):
            del payload[key]
        else:
            payload[key] = {"unexpected": [1, 2, 3]}
        return payload

    def value(self, schema: dict[str, object], root: dict[str, object]) -> Json:  # noqa: C901, PLR0911  # LW-150003 [C901, PLR0911]; one branch per JSON-schema keyword is the clearest shape for a schema interpreter; a dispatch table would hide the keyword order that decides precedence.
        """Draw one value for ``schema`` (resolving ``$ref`` against ``root``)."""
        rng = self._rng
        if "$ref" in schema:
            name = str(schema["$ref"]).rsplit("/", 1)[-1]
            definitions = cast("dict[str, dict[str, object]]", root.get("$defs", {}))
            return self.value(definitions[name], root)
        if "const" in schema:
            return cast("Json", schema["const"])
        if "enum" in schema:
            return cast("Json", rng.choice(cast("list[object]", schema["enum"])))
        for keyword in ("anyOf", "oneOf"):
            if keyword in schema:
                options = cast("list[dict[str, object]]", schema[keyword])
                return self.value(rng.choice(options), root)
        kind = schema.get("type")
        if kind == "object":
            return self._object(schema, root)
        if kind == "array":
            items = cast("dict[str, object]", schema.get("items", {}))
            low = cast("int", schema.get("minItems", 0))
            high = cast("int", schema.get("maxItems", low + 3))
            longest = min(high, low + (3 if self._bold else 1))
            return [self.value(items, root) for _ in range(rng.randint(low, longest))]
        if kind == "string":
            return self._string(schema)
        if kind == "integer":
            return rng.randint(
                cast("int", schema.get("minimum", -3)), cast("int", schema.get("maximum", 100))
            )
        if kind == "number":
            return rng.uniform(
                cast("float", schema.get("minimum", -1.0)),
                cast("float", schema.get("maximum", 1000.0)),
            )
        if kind == "boolean":
            return bool(rng.getrandbits(1))
        return None

    def _object(self, schema: dict[str, object], root: dict[str, object]) -> dict[str, Json]:
        properties = cast("dict[str, dict[str, object]]", schema.get("properties", {}))
        required = set(cast("list[str]", schema.get("required", [])))
        return {
            name: self.value(field, root)
            for name, field in properties.items()
            if name in required or (self._bold and self._rng.getrandbits(1))
        }

    def _string(self, schema: dict[str, object]) -> str:
        rng = self._rng
        low = cast("int", schema.get("minLength", 0))
        high = cast("int", schema.get("maxLength", 40))
        if self._bold and self._vocabulary and rng.random() < _REUSE_SHARE:
            text = rng.choice(self._vocabulary)
        else:
            alphabet = string.ascii_letters + string.digits + "-_ ./"
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 16)))
        text = text[:high]
        return text + "x" * max(0, low - len(text))
