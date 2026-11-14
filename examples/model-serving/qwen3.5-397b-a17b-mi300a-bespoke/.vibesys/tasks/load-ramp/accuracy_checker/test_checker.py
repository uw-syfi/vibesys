"""Hermetic tests for the load-ramp accuracy checker: no GPU, no server, no network.

The checker runs against a fake server, an in-memory stand-in for the candidate's HTTP
surface that answers from the task's own reference, with knobs that break it the ways a
candidate can (flipped top-1 tokens, a changed greedy completion, a missing endpoint).

    uv run pytest examples/model-serving/qwen3.5-397b-a17b-mi300a-bespoke/.vibesys/tasks/load-ramp/accuracy_checker/test_checker.py -q --no-cov -p no:tach
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checker

REFERENCE = checker.load_reference()


class _Reply:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._text = body if isinstance(body, str) else json.dumps(body)

    async def __aenter__(self) -> _Reply:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def text(self) -> str:
        return self._text


def _key(messages: tuple[dict, ...] | list[dict]) -> str:
    return json.dumps(list(messages), sort_keys=True)


class FakeServer:
    """The candidate's chat and score endpoints, answered from the reference.

    ``flipped`` is the set of (prompt index, position) whose top-1 token the server gets
    wrong; ``wrong_greedy`` the greedy prompt ids whose completion differs; without
    ``score_endpoint`` the server answers 404 there, as the seed engine on main did.
    """

    def __init__(
        self,
        *,
        flipped: frozenset[tuple[int, int]] = frozenset(),
        wrong_greedy: frozenset[str] = frozenset(),
        score_endpoint: bool = True,
        score_positions_short_by: int = 0,
    ) -> None:
        self.flipped = flipped
        self.wrong_greedy = wrong_greedy
        self.score_endpoint = score_endpoint
        self.score_positions_short_by = score_positions_short_by
        self.requests: list[str] = []
        self._greedy = {_key(p.messages): p for p in REFERENCE.greedy}
        self._scored = {
            (*p.prompt_ids, *p.continuation_ids): (i, p) for i, p in enumerate(REFERENCE.scored)
        }

    def post(self, url: str, json: dict) -> _Reply:  # noqa: A002  # LW-018706; aiohttp's keyword.
        # > The fake stands in for aiohttp.ClientSession.post, whose body keyword is `json`;
        # > renaming it would make the fake reject the calls the checker really makes.
        path = url.split("/", 3)[-1]
        self.requests.append(path)
        if path == "v1/score":
            return self._score(json) if self.score_endpoint else _Reply(404, "not found")
        return _Reply(200, self._chat(json))

    def _score(self, body: dict) -> _Reply:
        index, prompt = self._scored[tuple(body["prompt"])]
        assert body["continuation_start"] == len(prompt.prompt_ids)
        tokens = [
            token + 1 if (index, position) in self.flipped else token
            for position, token in enumerate(prompt.reference_top1)
        ]
        tokens = tokens[: len(tokens) - self.score_positions_short_by]
        rows = [{"token_id": t, "logprob": -0.1, "top_logprobs": [{"token_id": t}]} for t in tokens]
        return _Reply(200, {"scores": rows})

    def _chat(self, body: dict) -> dict:
        messages = tuple(body["messages"])
        greedy = self._greedy.get(_key(messages))
        if greedy is not None:
            text = greedy.text + (" (changed)" if greedy.prompt_id in self.wrong_greedy else "")
        else:
            text = self._probe_answer(messages)
        prompt_tokens = sum(len(m["content"].split()) for m in messages) + 3 * len(messages)
        return {
            "choices": [{"message": {"content": text}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 8},
        }

    @staticmethod
    def _probe_answer(messages: tuple[dict, ...]) -> str:
        question = messages[-1]["content"]
        arithmetic = re.match(r"What is (\d+) ([-+*]) (\d+)\?", question)
        if arithmetic:
            a, op, b = int(arithmetic[1]), arithmetic[2], int(arithmetic[3])
            return str({"+": a + b, "-": a - b, "*": a * b}[op])
        badge = re.search(r"badge code is ([A-Z]{2}-\w{4})", messages[0]["content"])
        if badge and "What was my badge code" in question:
            return badge[1]
        return "Understood, noted."


def _run(server: FakeServer) -> list[checker.GateOutcome]:
    return asyncio.run(checker.run_checks(server, "http://127.0.0.1:1", REFERENCE))


def _outcome(outcomes: list[checker.GateOutcome], name: str) -> checker.GateOutcome:
    return next(o for o in outcomes if o.name == name)


_POSITIONS = [(i, j) for i, p in enumerate(REFERENCE.scored) for j in range(len(p.reference_top1))]


class ReferenceTests(unittest.TestCase):
    def test_the_reference_is_the_campaigns_34_prompts_and_5_greedy_completions(self) -> None:
        self.assertEqual(len(REFERENCE.scored), 34)
        self.assertEqual(sum(len(p.reference_top1) for p in REFERENCE.scored), 1088)
        self.assertTrue(all(len(p.continuation_ids) == 32 for p in REFERENCE.scored))
        self.assertEqual(len(REFERENCE.greedy), checker.GREEDY_PROMPTS)
        self.assertTrue(all(p.text for p in REFERENCE.greedy))

    def test_a_malformed_reference_is_refused_naming_its_file(self) -> None:
        breakages = {
            "campaign_scores.json": lambda d: d["pins"].pop(),
            "campaign_greedy.json": lambda d: d["prompts"].pop(),
            "campaign_continuations.json": lambda d: d["pins"][0].update(prompt_ids="text"),
        }
        for name, breakage in breakages.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                for source in checker.REFERENCE_DIR.glob("campaign_*.json"):
                    shutil.copy(source, directory / source.name)
                data = json.loads((directory / name).read_text())
                breakage(data)
                (directory / name).write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "campaign_"):
                    checker.load_reference(directory)


class GateTests(unittest.TestCase):
    def test_a_server_that_matches_the_reference_passes_every_gate(self) -> None:
        outcomes = _run(FakeServer())
        self.assertEqual([o for o in outcomes if not o.ok], [])
        self.assertEqual(len(outcomes), 13 + 2)

    @settings(deadline=None, max_examples=25)
    @given(st.sets(st.sampled_from(_POSITIONS), max_size=40))
    def test_the_teacher_gate_passes_iff_at_least_1074_top1_tokens_agree(
        self, flipped: set[tuple[int, int]]
    ) -> None:
        gate = _outcome(_run(FakeServer(flipped=frozenset(flipped))), "teacher_top1")
        self.assertEqual(gate.ok, 1088 - len(flipped) >= 1074)
        self.assertIn(f"{1088 - len(flipped)}/1088", gate.detail)

    def test_the_threshold_is_exact_at_14_and_15_flips(self) -> None:
        for flips, ok in ((14, True), (15, False)):
            with self.subTest(flips=flips):
                server = FakeServer(flipped=frozenset(_POSITIONS[:flips]))
                self.assertIs(_outcome(_run(server), "teacher_top1").ok, ok)

    def test_a_server_without_the_score_endpoint_fails_the_teacher_gate(self) -> None:
        gate = _outcome(_run(FakeServer(score_endpoint=False)), "teacher_top1")
        self.assertFalse(gate.ok)
        self.assertIn("HTTP 404", gate.detail)

    def test_a_reply_scoring_fewer_positions_fails_even_if_every_token_agrees(self) -> None:
        gate = _outcome(_run(FakeServer(score_positions_short_by=1)), "teacher_top1")
        self.assertFalse(gate.ok)

    @settings(deadline=None, max_examples=10)
    @given(st.sets(st.sampled_from([p.prompt_id for p in REFERENCE.greedy]), min_size=1))
    def test_any_changed_greedy_completion_fails_the_greedy_gate(self, wrong: set[str]) -> None:
        gate = _outcome(_run(FakeServer(wrong_greedy=frozenset(wrong))), "greedy_reference")
        self.assertFalse(gate.ok)
        self.assertIn(f"{5 - len(wrong)}/5", gate.detail)


class MainTests(unittest.TestCase):
    def _main(self, workspace: Path, server: FakeServer, report: Path) -> int:
        class _Open:
            async def __aenter__(self) -> FakeServer:
                return server

            async def __aexit__(self, *exc_info: object) -> bool:
                return False

        args = argparse.Namespace(
            workspace=str(workspace), base_url="http://127.0.0.1:1", output_json=str(report)
        )
        return asyncio.run(checker.main_async(args, open_client=_Open))

    def test_a_clean_checkout_and_a_faithful_server_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "server.py").write_text("import torch\n")
            self.assertEqual(self._main(root, FakeServer(), root / "report.json"), 0)
            self.assertTrue(json.loads((root / "report.json").read_text())["passed"])

    def test_engine_code_in_the_checkout_fails_before_any_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "server.py").write_text("import vllm\n")
            server = FakeServer()
            self.assertEqual(self._main(root, server, root / "report.json"), 1)
            self.assertEqual(server.requests, [])


if __name__ == "__main__":
    unittest.main()
