"""CPU tests for the reference generator on a tiny random Qwen3.5-MoE in the real layout.

The tiny checkpoint has the served checkpoint's tensor names, per-expert MXFP4 experts and two
shards. The tests show that the generator's model is transformers' model on the dequantized
weights, that it writes the three files in the format the checker reads, that two runs write
the same bytes, and that the checker's numerics gates pass a server that is the reference.

Needs torch, transformers and safetensors (skipped without them):

    python -m pytest examples/model-serving/qwen3.5-397b-a17b-mi300a-bespoke/.vibesys/tasks/load-ramp/accuracy_checker/test_make_reference.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")
pytest.importorskip("safetensors")

from safetensors.torch import save_file  # noqa: E402  # LW-018721; after the skip guard.

# > These imports need torch and transformers; importorskip must run first so a missing
# > package skips the module instead of failing its collection.
from tokenizers import Tokenizer, models, pre_tokenizers  # noqa: E402  # LW-018726; as LW-018721.
from transformers import (  # noqa: E402  # LW-018722; as LW-018721.
    PreTrainedTokenizerFast,
    Qwen3_5MoeForCausalLM,
    Qwen3_5MoeTextConfig,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checker  # noqa: E402  # LW-018723; as LW-018721.
import make_reference  # noqa: E402  # LW-018724; as LW-018721.

FP4 = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0)
WORDS = ("<unk>", "<|im_end|>", "<|im_start|>", "user", "assistant", "system", "think")
VOCAB = 160
CPU = [torch.device("cpu")]


def _config() -> Qwen3_5MoeTextConfig:
    return Qwen3_5MoeTextConfig(
        vocab_size=VOCAB, hidden_size=64, num_hidden_layers=4,
        layer_types=["linear_attention", "linear_attention", "full_attention", "linear_attention"],
        num_attention_heads=4, num_key_value_heads=2, head_dim=16,
        linear_num_key_heads=2, linear_num_value_heads=4, linear_key_head_dim=16, linear_value_head_dim=16,
        moe_intermediate_size=32, shared_expert_intermediate_size=32, num_experts=8, num_experts_per_tok=3,
        eos_token_id=1, tie_word_embeddings=False, max_position_embeddings=16384, attn_implementation="eager", experts_implementation="eager",
        rope_parameters={"rope_type": "default", "rope_theta": 10000.0, "partial_rotary_factor": 0.5},
    )  # fmt: skip


def _quantize(w: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """(packed uint8, e8m0 scales, dense reconstruction) of a [N, K] weight."""
    n, k = w.shape
    blocks = w.reshape(n, k // 32, 32)
    exp = torch.floor(torch.log2(blocks.abs().amax(-1).clamp_min(1e-30))) - 2
    scale = torch.exp2(exp)[..., None]
    code = ((blocks.abs() / scale)[..., None] - torch.tensor(FP4)).abs().argmin(-1)
    code = code + 8 * (blocks < 0)
    recon = torch.tensor(FP4 + tuple(-v for v in FP4))[code] * scale
    code = code.reshape(n, k).to(torch.uint8)
    packed = code[:, 0::2] | (code[:, 1::2] << 4)
    return packed, (exp + 127).reshape(n, k // 32).to(torch.uint8), recon.reshape(n, k)


def _hf_model() -> Qwen3_5MoeForCausalLM:
    """A random tiny model whose expert weights are exactly representable in MXFP4."""
    torch.manual_seed(0)
    model = Qwen3_5MoeForCausalLM(_config()).eval()
    with torch.no_grad():
        for name, p in model.named_parameters():
            if "A_log" not in name and "dt_bias" not in name:
                p.normal_(0, 0.3 if p.dim() > 1 else 0.1)
        for layer in model.model.layers:
            for p in (layer.mlp.experts.gate_up_proj, layer.mlp.experts.down_proj):
                for e in range(p.shape[0]):
                    p[e] = _quantize(p[e])[2]
    return model


def _write_checkpoint(model: Qwen3_5MoeForCausalLM, out: Path) -> None:
    tensors: dict[str, torch.Tensor] = {}
    for name, p in model.state_dict().items():
        name = name.replace("model.", make_reference.CHECKPOINT_PREFIX, 1)
        if "experts.gate_up_proj" in name or "experts.down_proj" in name:
            base = name.rsplit("experts.", 1)[0]
            for e in range(p.shape[0]):
                half = p.shape[1] // 2
                parts = (
                    {"gate_proj": p[e][:half], "up_proj": p[e][half:]}
                    if "gate_up" in name
                    else {"down_proj": p[e]}
                )
                for proj, w in parts.items():
                    packed, scale, _ = _quantize(w)
                    tensors[f"{base}experts.{e}.{proj}.weight"] = packed
                    tensors[f"{base}experts.{e}.{proj}.weight_scale"] = scale
        else:
            tensors[name] = p.detach().clone()
    keys = sorted(tensors)
    for i, part in enumerate((keys[::2], keys[1::2])):
        save_file({k: tensors[k].contiguous() for k in part}, str(out / f"model-{i}.safetensors"))
    (out / "config.json").write_text(json.dumps({"text_config": model.config.to_dict()}))


def _tokenizer() -> PreTrainedTokenizerFast:
    vocab = {w: i for i, w in enumerate(WORDS + tuple(f"w{i}" for i in range(VOCAB - len(WORDS))))}
    core = Tokenizer(models.WordLevel(vocab, unk_token="<unk>"))
    core.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    template = (
        "{% for m in messages %}<|im_start|> {{ m.role }} {{ m.content }} <|im_end|> {% endfor %}"
        "{% if add_generation_prompt %}<|im_start|> assistant "
        "{% if enable_thinking %}think {% endif %}{% endif %}"
    )
    return PreTrainedTokenizerFast(
        tokenizer_object=core, unk_token="<unk>", eos_token="<|im_end|>", chat_template=template
    )


@pytest.fixture(scope="module")
def checkpoint(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("checkpoint")
    _write_checkpoint(_hf_model(), directory)
    return directory


def _generate_files(checkpoint: Path, out: Path) -> dict[str, str]:
    """One whole generator run: load, build over the real prompt set, write."""
    tokenizer = _tokenizer()
    model = make_reference.load_reference_model(checkpoint, CPU, torch.float32)
    stop = make_reference.stop_token_ids(checkpoint, tokenizer)
    files = make_reference.build_reference(
        model, tokenizer, stop, make_reference.reference_prompts()
    )
    return make_reference.write_reference(files, out)


@pytest.fixture(scope="module")
def generated(checkpoint: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("reference")
    _generate_files(checkpoint, out)
    return out


def test_dequant_decodes_low_nibble_first_with_e8m0_scales() -> None:
    packed = torch.tensor([0x21, 0x73, 0x89, *([0] * 13)], dtype=torch.uint8)
    out = make_reference.dequant_mxfp4(
        packed, torch.tensor([128], dtype=torch.uint8), torch.float32
    )
    assert out[:6].tolist() == [1.0, 2.0, 3.0, 12.0, -1.0, -0.0]
    assert out[6:].abs().sum() == 0


def test_the_prompts_are_the_committed_references_prompts() -> None:
    rows = json.loads((checker.REFERENCE_DIR / "campaign_continuations.json").read_text())["pins"]
    assert [r["messages"] for r in rows] == make_reference.reference_prompts()


def test_the_loaded_model_is_transformers_on_the_dequantized_weights(checkpoint: Path) -> None:
    oracle = _hf_model()
    mine = make_reference.load_reference_model(checkpoint, CPU, torch.float32)
    ids = torch.randint(2, VOCAB, (1, 40), generator=torch.Generator().manual_seed(3))
    with torch.no_grad():
        want, got = oracle(input_ids=ids).logits, mine(input_ids=ids).logits
    assert torch.equal(got, want)


def test_the_files_have_the_checkers_format(generated: Path) -> None:
    reference = checker.load_reference(generated)
    assert len(reference.scored) == 34
    assert sum(len(p.reference_top1) for p in reference.scored) == checker.TEACHER_TOP1_TOTAL
    assert len(reference.greedy) == checker.GREEDY_PROMPTS


def test_two_runs_write_the_same_bytes(checkpoint: Path, generated: Path, tmp_path: Path) -> None:
    digests = _generate_files(checkpoint, tmp_path)
    for name, digest in digests.items():
        assert (generated / name).read_bytes() == (tmp_path / name).read_bytes(), name
        assert len(digest) == 64


class _Reply:
    def __init__(self, body: dict) -> None:
        self.status, self._text = 200, json.dumps(body)

    async def __aenter__(self) -> _Reply:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def text(self) -> str:
        return self._text


class _ReferenceServer:
    """The checker's HTTP surface answered by the generator's own model, in process."""

    def __init__(self, checkpoint: Path) -> None:
        self.tokenizer = _tokenizer()
        self.model = make_reference.load_reference_model(checkpoint, CPU, torch.float32)
        self.stop = make_reference.stop_token_ids(checkpoint, self.tokenizer)

    def post(self, url: str, json: dict) -> _Reply:  # noqa: A002  # LW-018727; aiohttp's keyword.
        # > The stand-in replaces aiohttp.ClientSession.post, whose body keyword is `json`.
        if url.endswith("/v1/score"):
            start = json["continuation_start"]
            prompt, continuation = json["prompt"][:start], json["prompt"][start:]
            top1, logprobs = make_reference.teacher_forced(self.model, prompt, continuation)
            rows = [
                {"token_id": t, "logprob": lp, "top_logprobs": [{"token_id": top, "logprob": 0}]}
                for t, lp, top in zip(continuation, logprobs, top1, strict=True)
            ]
            return _Reply({"scores": rows})
        prompt = make_reference.prompt_token_ids(self.tokenizer, json["messages"])
        ids = make_reference.generate(
            self.model, prompt, json["max_tokens"], min_new=0, stop=self.stop
        )
        ids = ids[: next((i for i, t in enumerate(ids) if t in self.stop), len(ids))]
        text = self.tokenizer.decode(ids, skip_special_tokens=True)
        return _Reply({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


def test_the_checkers_numerics_gates_pass_the_reference_itself(
    checkpoint: Path, generated: Path
) -> None:
    reference = checker.load_reference(generated)
    server = _ReferenceServer(checkpoint)
    teacher = asyncio.run(checker.gate_teacher_top1(server, "http://ref", reference.scored))
    greedy = asyncio.run(checker.gate_greedy_reference(server, "http://ref", reference.greedy))
    assert teacher.ok, teacher.detail
    assert "1088/1088" in teacher.detail
    assert greedy.ok, greedy.detail
