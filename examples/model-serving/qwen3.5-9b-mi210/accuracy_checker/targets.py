"""Candidate adapters the gate can evaluate.
A target answers two questions for a token-id prompt:
- `greedy(prompt, n)`: exactly n greedy tokens, EOS ignored.
- `teacher_forced(prompt, cont)`: for each position of `cont`, the logprob the
  candidate assigns to that given token and the candidate's own argmax, when
  conditioned on `prompt + cont[:i]`.
`HttpTarget` needs only the OpenAI completions surface plus the vLLM extensions
`ignore_eos`, `return_token_ids`, and `echo` + `logprobs` with
`return_tokens_as_token_ids` (vLLM itself satisfies it too). Its `complete`
also returns `usage.prompt_tokens_details.cached_tokens` for the resume check.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol, cast

import httpx


@dataclass(frozen=True)
class ForcedStep:
    logprob: float  # candidate logprob of the given token
    argmax: int  # candidate's top-1 token


@dataclass(frozen=True)
class Completion:
    token_ids: list[int]
    # usage.prompt_tokens_details.cached_tokens; None if the server omitted it.
    cached_tokens: int | None


class Target(Protocol):
    name: str

    def greedy(self, prompt: list[int], n: int) -> list[int]: ...
    def teacher_forced(self, prompt: list[int], cont: list[int]) -> list[ForcedStep]: ...


class EngineTarget:
    """The in-process reference engine (or any engine exposing the same `generate`/`score`)."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.name = "inproc"

    def greedy(self, prompt: list[int], n: int) -> list[int]:
        from reference.engine import SamplingParams

        params = SamplingParams(max_tokens=n, temperature=0.0, ignore_eos=True)
        return [s.token_id for s in self.engine.generate(prompt, params)]

    def teacher_forced(self, prompt: list[int], cont: list[int]) -> list[ForcedStep]:
        scored = self.engine.score(prompt + cont, top_k=1)[len(prompt) - 1 :]
        return [ForcedStep(s.logprob, s.top[0][0]) for s in scored]


class HttpTarget:
    """A running OpenAI-compatible server."""

    def __init__(self, base_url: str, model: str, timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = f"http:{self.base_url}"
        self.client = httpx.Client(timeout=timeout)

    def _post(self, body: dict) -> dict:
        r = self.client.post(f"{self.base_url}/v1/completions", json={"model": self.model, **body})
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code} from server: {r.text[:500]}")
        return r.json()

    def complete(self, prompt: list[int], n: int) -> Completion:
        body = {
            "prompt": prompt,
            "max_tokens": n,
            "temperature": 0,
            "ignore_eos": True,
            "return_token_ids": True,
        }
        resp = self._post(body)
        ids = resp["choices"][0].get("token_ids")
        if ids is None:
            raise RuntimeError(
                "server did not return choices[0].token_ids (needs return_token_ids support)"
            )
        details = (resp.get("usage") or {}).get("prompt_tokens_details") or {}
        return Completion(ids, details.get("cached_tokens"))

    def greedy(self, prompt: list[int], n: int) -> list[int]:
        return self.complete(prompt, n).token_ids

    def teacher_forced(self, prompt: list[int], cont: list[int]) -> list[ForcedStep]:
        body = {
            "prompt": prompt + cont,
            "max_tokens": 1,
            "temperature": 0,
            "echo": True,
            "logprobs": 1,
            "return_tokens_as_token_ids": True,
        }
        lp = self._post(body)["choices"][0]["logprobs"]
        span = slice(len(prompt), len(prompt) + len(cont))
        steps = []
        for given, top in zip(lp["token_logprobs"][span], lp["top_logprobs"][span], strict=True):
            best = max(top.items(), key=lambda kv: kv[1])[0]
            steps.append(ForcedStep(float(given), int(best.removeprefix("token_id:"))))
        return steps

    def stream_matches(self, prompt: list[int], n: int, expected: list[int]) -> str | None:
        """Protocol check: streamed token ids and usage agree with the non-streamed result."""
        body = {
            "model": self.model,
            "prompt": prompt,
            "max_tokens": n,
            "temperature": 0,
            "ignore_eos": True,
            "return_token_ids": True,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        ids: list[int] = []
        usage = None
        done = False
        with self.client.stream("POST", f"{self.base_url}/v1/completions", json=body) as r:
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: ") :]
                if data == "[DONE]":
                    done = True
                    break
                chunk = json.loads(data)
                for c in chunk.get("choices", []):
                    ids.extend(c.get("token_ids") or [])
                usage = chunk.get("usage") or usage
        if not done:
            return "stream did not end with data: [DONE]"
        if ids != expected:
            return (
                f"streamed ids differ from non-streamed ids (first 8: {ids[:8]} vs {expected[:8]})"
            )
        if (
            not usage
            or usage.get("completion_tokens") != n
            or usage.get("prompt_tokens") != len(prompt)
        ):
            return f"bad usage chunk: {usage}"
        return None


class HFTarget:
    """HF transformers `Qwen3_5ForCausalLM` in bf16: the golden source, also used to calibrate noise.
    Greedy decoding is a manual argmax loop over HF's own KV/state cache, which is
    what `generate(do_sample=False)` does, minus EOS stopping.
    `use_fla=False` hides flash-linear-attention so HF falls back to its torch GDN.
    """

    def __init__(self, model: str, use_fla: bool = True, device: str = "cuda") -> None:
        if not use_fla:
            # A None entry in sys.modules makes the import raise and find_spec return
            # None (docs.python.org/3/reference/import.html#the-module-cache); typeshed
            # types the values as ModuleType only.
            modules = cast("dict[str, ModuleType | None]", sys.modules)
            for mod in ("fla", "causal_conv1d"):
                if modules.get(mod) is not None:
                    raise RuntimeError("use_fla=False must be set before fla is imported")
                modules[mod] = None
        import torch
        from transformers import AutoModelForCausalLM

        self.torch = torch
        self.model = AutoModelForCausalLM.from_pretrained(
            model, dtype=torch.bfloat16, device_map=device
        ).eval()
        self.device = device
        self.name = f"hf(fla={'on' if use_fla else 'off'})"

    def greedy(self, prompt: list[int], n: int) -> list[int]:
        torch = self.torch
        with torch.inference_mode():
            ids = torch.tensor([prompt], device=self.device)
            out = self.model(input_ids=ids, use_cache=True)
            past = out.past_key_values
            toks: list[int] = []
            for _ in range(n):
                nxt = int(out.logits[0, -1].float().argmax())
                toks.append(nxt)
                if len(toks) == n:
                    break
                out = self.model(
                    input_ids=torch.tensor([[nxt]], device=self.device),
                    past_key_values=past,
                    use_cache=True,
                )
                past = out.past_key_values
            return toks

    def forced_logprobs(self, prompt: list[int], cont: list[int]):
        """Full-vocab logprobs [len(cont), vocab] (fp32) predicting each token of `cont`."""
        torch = self.torch
        with torch.inference_mode():
            ids = torch.tensor([prompt + cont], device=self.device)
            logits = (
                self.model(input_ids=ids, use_cache=False).logits[0, len(prompt) - 1 : -1].float()
            )
            return torch.log_softmax(logits, dim=-1)

    def teacher_forced(self, prompt: list[int], cont: list[int]) -> list[ForcedStep]:
        lps = self.forced_logprobs(prompt, cont)
        given = lps.gather(1, self.torch.tensor(cont, device=self.device)[:, None])[:, 0].tolist()
        return [ForcedStep(g, a) for g, a in zip(given, lps.argmax(-1).tolist(), strict=True)]
