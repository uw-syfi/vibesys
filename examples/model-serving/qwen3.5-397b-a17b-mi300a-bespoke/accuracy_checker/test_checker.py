"""Hermetic tests for the pin gate and the probes (in-process fake server, no GPU).

uv run pytest examples/model-serving/qwen3.5-397b-a17b-mi300a-bespoke/accuracy_checker/test_checker.py -q --no-cov -p no:tach
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import checker
except Exception as exc:
    checker = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

N = 32


def _pins_payload(n_pins: int = 10) -> dict:
    return {
        "version": 1,
        "pins": [
            {
                "id": f"p{i}",
                "messages": [{"role": "user", "content": "hi"}],
                "expected_token_ids": list(range(N)),
            }
            for i in range(n_pins)
        ],
    }


@unittest.skipIf(checker is None, f"could not import checker.py: {_IMPORT_ERROR}")
class FirstDivergenceTests(unittest.TestCase):
    def test_full_match(self) -> None:
        self.assertEqual(checker.first_divergence(list(range(N)), list(range(N))), N)

    def test_mismatch_index(self) -> None:
        got = list(range(N))
        got[5] = 999
        self.assertEqual(checker.first_divergence(list(range(N)), got), 5)

    def test_one_token_short_is_tolerated(self) -> None:
        self.assertEqual(checker.first_divergence(list(range(N)), list(range(N - 1))), N)

    def test_much_shorter_diverges_at_its_length(self) -> None:
        self.assertEqual(checker.first_divergence(list(range(N)), list(range(10))), 10)

    def test_longer_output_is_fine(self) -> None:
        self.assertEqual(checker.first_divergence(list(range(N)), [*range(N), 7]), N)


@unittest.skipIf(checker is None, f"could not import checker.py: {_IMPORT_ERROR}")
class ScorePinsTests(unittest.TestCase):
    def _div(self, exact: int, late: int = 0, early: int = 0) -> dict[str, int]:
        out = {f"e{i}": N for i in range(exact)}
        out |= {f"l{i}": checker.MIN_DIVERGENCE_POSITION for i in range(late)}
        out |= {f"x{i}": checker.MIN_DIVERGENCE_POSITION - 1 for i in range(early)}
        return out

    def test_all_exact_passes(self) -> None:
        self.assertTrue(checker.score_pins(self._div(10)).ok)

    def test_nine_of_ten_with_late_divergence_passes(self) -> None:
        self.assertTrue(checker.score_pins(self._div(9, late=1)).ok)

    def test_eight_of_ten_fails_on_fraction(self) -> None:
        self.assertFalse(checker.score_pins(self._div(8, late=2)).ok)

    def test_one_early_divergence_fails_even_with_high_fraction(self) -> None:
        outcome = checker.score_pins(self._div(19, early=1))
        self.assertFalse(outcome.ok)
        self.assertIn("early divergence", outcome.detail)

    def test_empty_fails(self) -> None:
        self.assertFalse(checker.score_pins({}).ok)


@unittest.skipIf(checker is None, f"could not import checker.py: {_IMPORT_ERROR}")
class LoadPinsTests(unittest.TestCase):
    def test_missing_file_names_pins_json_and_generator(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            checker.load_pins(Path("/nonexistent/reference/pins.json"))
        message = str(ctx.exception)
        self.assertIn("reference/pins.json", message)
        self.assertIn("make_pins.py", message)
        self.assertIn("--skip-pins", message)

    def test_valid_file_loads_and_truncates_to_prefix(self) -> None:
        payload = _pins_payload()
        payload["pins"][0]["expected_token_ids"] = list(range(N + 10))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pins.json"
            path.write_text(json.dumps(payload))
            pins = checker.load_pins(path)
        self.assertEqual(len(pins), 10)
        self.assertEqual(len(pins[0].expected_token_ids), checker.EXACT_PREFIX_TOKENS)

    def test_short_expected_ids_rejected(self) -> None:
        payload = _pins_payload()
        payload["pins"][2]["expected_token_ids"] = [1, 2, 3]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pins.json"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeError, r"pins\[2\]"):
                checker.load_pins(path)

    def test_too_few_pins_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pins.json"
            path.write_text(json.dumps(_pins_payload(2)))
            with self.assertRaisesRegex(RuntimeError, "at least"):
                checker.load_pins(path)


class _FakeTokenizer:
    """Maps chr(65 + id) text back to ids, so the fake server can emit text."""

    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        return [ord(c) - 65 for c in text]


class _FakePost:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> _FakePost:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def text(self) -> str:
        return json.dumps(self._payload)


class _FakeClient:
    """Records request bodies and answers with a fixed text."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.bodies: list[dict] = []

    def post(self, url: str, json: dict, timeout: object) -> _FakePost:
        self.bodies.append(json)
        return _FakePost(
            {"choices": [{"message": {"content": self._text}, "finish_reason": "length"}]}
        )


@unittest.skipIf(checker is None, f"could not import checker.py: {_IMPORT_ERROR}")
class GatePinsTests(unittest.TestCase):
    def _pins(self) -> list:
        return [
            checker.Pin(f"p{i}", [{"role": "user", "content": "hi"}], list(range(N)))
            for i in range(10)
        ]

    def test_matching_server_passes_and_request_is_greedy_no_thinking(self) -> None:
        client = _FakeClient("".join(chr(65 + i) for i in range(N)))
        outcomes = asyncio.run(
            checker.gate_pins(client, "http://fake", self._pins(), _FakeTokenizer())
        )
        self.assertTrue(outcomes[0].ok, outcomes[0].detail)
        body = client.bodies[0]
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], N)
        self.assertIs(body["ignore_eos"], True)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    def test_garbage_server_fails(self) -> None:
        client = _FakeClient("ZZZZ")
        outcomes = asyncio.run(
            checker.gate_pins(client, "http://fake", self._pins(), _FakeTokenizer())
        )
        self.assertFalse(outcomes[0].ok)


if __name__ == "__main__":
    unittest.main()
