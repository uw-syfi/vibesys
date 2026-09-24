"""Hermetic CPU tests for the seed: HF parity on a tiny random model, MXFP4 dequant, HTTP smoke.
Run with a python that has torch, transformers, safetensors, aiohttp, pytest:
    /tmp/torchenv/bin/python -m pytest examples/model-serving/qwen3.5-397b-a17b-mi300a-bespoke/seed_tests -p no:cacheprovider --no-cov
"""

import http.client
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import torch
from safetensors.torch import save_file
from transformers import Qwen3_5MoeForCausalLM, Qwen3_5MoeTextConfig

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# lint-waiver: LW-008012 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
import model as seed_model  # noqa: E402

# lint-waiver: LW-008013 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
from mxfp4 import dequant_mxfp4  # noqa: E402

FP4 = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]
VOCAB = 128


def tiny_config() -> Qwen3_5MoeTextConfig:
    return Qwen3_5MoeTextConfig(
        vocab_size=VOCAB, hidden_size=64, num_hidden_layers=4,
        layer_types=["linear_attention", "linear_attention", "full_attention", "linear_attention"],
        num_attention_heads=4, num_key_value_heads=2, head_dim=16,
        linear_num_key_heads=2, linear_num_value_heads=4, linear_key_head_dim=16, linear_value_head_dim=16,
        moe_intermediate_size=32, shared_expert_intermediate_size=32, num_experts=8, num_experts_per_tok=3,
        eos_token_id=1, rope_parameters={"rope_type": "default", "rope_theta": 10000.0, "partial_rotary_factor": 0.5},
    )  # fmt: skip


def quantize_mxfp4(w: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Test-local MXFP4 quantizer: returns (packed u8, e8m0 scale u8, dense reconstruction)."""
    n, k = w.shape
    blocks = w.reshape(n, k // 32, 32)
    exp = torch.floor(torch.log2(blocks.abs().amax(-1).clamp_min(1e-30))) - 2  # so max maps to <= 6
    scale = torch.exp2(exp)[..., None]
    mags = (blocks.abs() / scale)[..., None] - torch.tensor(FP4)
    code = mags.abs().argmin(-1) + 8 * (blocks < 0)
    recon = torch.tensor(FP4 + [-v for v in FP4])[code] * scale
    code = code.reshape(n, k).to(torch.uint8)
    packed = code[:, 0::2] | (code[:, 1::2] << 4)
    return packed, (exp + 127).reshape(n, k // 32).to(torch.uint8), recon.reshape(n, k)


def build_hf(seed: int = 0) -> Qwen3_5MoeForCausalLM:
    torch.manual_seed(seed)
    cfg = tiny_config()
    cfg._attn_implementation = "eager"
    m = Qwen3_5MoeForCausalLM(cfg).eval()
    with torch.no_grad():
        for name, p in m.named_parameters():
            if "A_log" not in name and "dt_bias" not in name:
                p.normal_(0, 0.3 if p.dim() > 1 else 0.1)
    return m


def write_checkpoint(m: Qwen3_5MoeForCausalLM, out: Path, *, mxfp4: bool) -> None:
    """Save HF weights under the real checkpoint's names (per-expert tensors, two shards)."""
    tensors: dict[str, torch.Tensor] = {}
    for name, p in m.state_dict().items():
        p = p.detach().clone()
        name = (
            name.replace("model.", "model.language_model.", 1)
            if name.startswith("model.")
            else name
        )
        if "experts.gate_up_proj" in name or "experts.down_proj" in name:
            base = name.rsplit("experts.", 1)[0]
            for e in range(p.shape[0]):
                parts = (
                    {"gate_proj": p[e][: p.shape[1] // 2], "up_proj": p[e][p.shape[1] // 2 :]}
                    if "gate_up" in name
                    else {"down_proj": p[e]}
                )
                for proj, w in parts.items():
                    tensors.update(expert_entries(f"{base}experts.{e}.{proj}", w, mxfp4=mxfp4))
        else:
            tensors[name] = p
    keys = sorted(tensors)
    for i, part in enumerate((keys[::2], keys[1::2])):
        save_file({k: tensors[k].contiguous() for k in part}, str(out / f"model-{i}.safetensors"))
    (out / "config.json").write_text(json.dumps({"text_config": m.config.to_dict()}))


def expert_entries(prefix: str, w: torch.Tensor, *, mxfp4: bool) -> dict[str, torch.Tensor]:
    if not mxfp4:
        return {f"{prefix}.weight": w}
    packed, scale, _ = quantize_mxfp4(w)
    return {f"{prefix}.weight": packed, f"{prefix}.weight_scale": scale}


def quantize_hf_in_place(m: Qwen3_5MoeForCausalLM) -> None:
    """Replace the HF expert weights by their MXFP4 reconstruction so HF is the fp reference."""
    with torch.no_grad():
        for layer in m.model.layers:
            ex = layer.mlp.experts
            for p in (ex.gate_up_proj, ex.down_proj):
                for e in range(p.shape[0]):
                    p[e] = quantize_mxfp4(p[e])[2]


# ---------------------------------------------------------------- tests
def test_mxfp4_dequant_hand_vector() -> None:
    packed = torch.zeros(16, dtype=torch.uint8)
    packed[0], packed[1] = 0x21, 0x73  # low nibble first: 1,2 then 3,7 -> 0.5, 1.0, 1.5, 6.0
    packed[2] = 0x89  # sign bit: 9 -> -0.5, 8 -> -0.0
    out = dequant_mxfp4(
        packed, torch.tensor([128], dtype=torch.uint8), torch.float32
    )  # scale 2^(128-127)
    assert out.shape == (32,)
    assert out[:6].tolist() == [1.0, 2.0, 3.0, 12.0, -1.0, -0.0]
    assert out[6:].abs().sum() == 0
    half = dequant_mxfp4(
        packed, torch.tensor([125], dtype=torch.uint8), torch.float32
    )  # scale 0.25
    assert half[3].item() == 1.5


def test_mxfp4_roundtrip_matches_test_quantizer() -> None:
    w = torch.randn(4, 64)
    packed, scale, recon = quantize_mxfp4(w)
    assert torch.equal(dequant_mxfp4(packed, scale, torch.float32), recon)


@pytest.mark.parametrize("mxfp4", [False, True])
def test_prefill_and_greedy_decode_match_hf(
    tmp_path: Path, mxfp4: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    hf = build_hf()
    if mxfp4:
        quantize_hf_in_place(hf)
    write_checkpoint(hf, tmp_path, mxfp4=mxfp4)
    monkeypatch.setattr(
        seed_model, "PREFILL_CHUNK", 4
    )  # exercise chunked prefill over static state
    mine = seed_model.Model(tmp_path, ["cpu"], torch.float32, max_seq=64)
    ids = torch.randint(2, VOCAB, (1, 11))
    with torch.no_grad():
        ref = hf(input_ids=ids).logits
    mine.reset()
    assert torch.allclose(mine.forward(ids, 0, all_logits=True), ref[0], atol=1e-4, rtol=1e-4)
    mine.reset()  # incremental: prefill in chunks, then 8 greedy steps against HF full recompute
    for s in range(0, ids.shape[1], 4):
        got = mine.forward(ids[:, s : s + 4], s)
    seq = ids
    for step in range(8):
        with torch.no_grad():
            ref_logits = hf(input_ids=seq).logits[0, -1]
        assert torch.allclose(got[-1], ref_logits, atol=1e-4, rtol=1e-4), f"step {step}"
        nxt = ref_logits.argmax().view(1, 1)
        assert int(got[-1].argmax()) == int(nxt)
        got = mine.forward(nxt, seq.shape[1])
        seq = torch.cat([seq, nxt], dim=1)
    assert list(mine.generate(ids[0].tolist(), 8, 0.0, frozenset())) == seq[0, 11:].tolist()


def test_http_smoke(tmp_path: Path) -> None:
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import PreTrainedTokenizerFast

    write_checkpoint(build_hf(), tmp_path, mxfp4=False)
    words = [
        "<unk>",
        "<|im_end|>",
        "<|im_start|>",
        "user",
        "assistant",
        "system",
        "hello",
        "world",
        "think",
    ]
    vocab = {w: i for i, w in enumerate(words + [f"w{i}" for i in range(VOCAB - len(words))])}
    core = Tokenizer(models.WordLevel(vocab, unk_token="<unk>"))
    core.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    template = (
        "{% for m in messages %}<|im_start|> {{ m.role }} {{ m.content }} <|im_end|> {% endfor %}"
        "{% if add_generation_prompt %}<|im_start|> assistant {% if enable_thinking %}think {% endif %}{% endif %}"
    )
    tok = PreTrainedTokenizerFast(
        tokenizer_object=core, unk_token="<unk>", eos_token="<|im_end|>", chat_template=template
    )
    tok.save_pretrained(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cmd = [sys.executable, str(ROOT / "server.py"), "--model-path", str(tmp_path), "--host", "127.0.0.1", "--port", str(port),
           "--devices", "cpu", "--dtype", "float32", "--max-seq-len", "64"]  # fmt: skip
    proc = subprocess.Popen(cmd, cwd=ROOT)
    try:
        wait_healthy(port)
        body = {"messages": [{"role": "user", "content": "hello world"}], "max_tokens": 6, "temperature": 0, "ignore_eos": True,
                "chat_template_kwargs": {"enable_thinking": True}}  # fmt: skip
        status, data = post(port, body)
        reply = json.loads(data)
        assert status == 200
        assert reply["usage"]["completion_tokens"] == 6
        assert reply["choices"][0]["finish_reason"] == "length"
        assert (
            reply["usage"]["prompt_tokens"] == 8
        )  # <|im_start|> user hello world <|im_end|> <|im_start|> assistant think
        status, data = post(
            port, body | {"stream": True, "stream_options": {"include_usage": True}}
        )
        events = [line[6:] for line in data.decode().splitlines() if line.startswith("data: ")]
        assert status == 200
        assert events[-1] == "[DONE]"
        chunks = [json.loads(e) for e in events[:-1]]
        content = [
            c
            for c in chunks
            if c["choices"] and c["choices"][0]["delta"].get("content") is not None
        ]
        assert len(content) == 6  # one SSE chunk per generated token
        assert chunks[-2]["choices"][0]["finish_reason"] == "length"
        assert chunks[-1]["usage"]["completion_tokens"] == 6
        assert (
            "".join(c["choices"][0]["delta"]["content"] for c in content)
            == reply["choices"][0]["message"]["content"]
        )
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def post(port: int, body: dict) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request(
        "POST", "/v1/chat/completions", json.dumps(body), {"Content-Type": "application/json"}
    )
    r = conn.getresponse()
    return r.status, r.read()


def wait_healthy(port: int, timeout: float = 180) -> None:
    deadline, saw_503 = time.time() + timeout, False
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/health")
            status = conn.getresponse().status
            if status == 200:
                return
            saw_503 = saw_503 or status == 503
        except OSError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"server not healthy in {timeout}s (saw 503: {saw_503})")
