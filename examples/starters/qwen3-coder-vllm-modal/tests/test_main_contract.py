import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


def test_prompt_token_details_are_enabled():
    usage = main._usage(prompt_tokens=7, completion_tokens=3, cached_tokens=5)

    assert main.ENABLE_PROMPT_TOKENS_DETAILS is True
    assert usage["prompt_tokens"] == 7
    assert usage["completion_tokens"] == 3
    assert usage["total_tokens"] == 10
    assert usage["prompt_tokens_details"]["cached_tokens"] == 5


def test_cached_tokens_from_vllm_output_prefers_nonzero_value():
    class Metrics:
        num_cached_tokens = 48

    class Output:
        num_cached_tokens = 0
        metrics = Metrics()

    assert main._cached_tokens_from_output(Output(), fallback=16) == 48


def test_prompt_cache_fallback_reports_block_aligned_repeat_hit():
    class FakeServer:
        cache_block_size = 16
        _completed_prompt_prefixes = set()

    prompt_ids = list(range(1500))

    assert main.Server._prompt_cache_hit_tokens(FakeServer, prompt_ids) == 0

    main.Server._record_prompt_cache_prefixes(FakeServer, prompt_ids)

    assert main.Server._prompt_cache_hit_tokens(FakeServer, prompt_ids) == 1488


def test_trace_completion_cap_preserves_best_measured_tail_latency(monkeypatch):
    class FakeSamplingParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setitem(
        sys.modules,
        "vllm",
        type("FakeVllm", (), {"SamplingParams": FakeSamplingParams}),
    )

    params = main.Server._sampling_params(object(), {"max_tokens": 9000})

    assert main.VLLM_MAX_COMPLETION_TOKENS == 4096
    assert params.kwargs["max_tokens"] == 4096


def test_request_annotations_are_not_postponed():
    source = Path(main.__file__).read_text()

    assert "from __future__ import annotations" not in source
    assert "from fastapi import FastAPI, HTTPException, Request" in source
    assert "async def completions(request: Request)" in source
    assert "async def chat_completions(request: Request)" in source


def test_modal_class_batches_requests_inside_one_h100_container():
    source = Path(main.__file__).read_text()

    assert "max_containers=1" in source
    assert "@modal.concurrent(max_inputs=32, target_inputs=8)" in source


def test_message_validation_accepts_checker_shape():
    messages = [{"role": "user", "content": "What is 1 + 1?"}]

    assert main._coerce_messages(messages) == messages


def test_completion_sse_chunk_shape():
    chunk = {
        "id": main._request_id("cmpl"),
        "object": "text_completion",
        "created": 0,
        "model": main.MODEL_ID,
        "choices": [{"text": "Paris", "index": 0, "finish_reason": None}],
    }
    line = f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"

    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    assert json.loads(line[len("data: ") :])["choices"][0]["text"] == "Paris"


def test_completion_stream_records_token_prompt_prefix_at_admission(monkeypatch):
    class FakeSamplingParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeTokenizer:
        def encode(self, text, add_special_tokens=False):
            assert add_special_tokens is False
            return [99] if text == "x" else []

    class FakeChoice:
        text = "x"
        finish_reason = None

    class FakeOutput:
        outputs = [FakeChoice()]

    class FakeEngine:
        def __init__(self):
            self.prompts = []

        async def generate(self, prompt, sampling_params, request_id):
            self.prompts.append(prompt)
            yield FakeOutput()

    class FakeServer:
        tokenizer = FakeTokenizer()
        engine = FakeEngine()
        cache_block_size = 16
        _completed_prompt_prefixes = set()
        _prompt_arg = main.Server._prompt_arg.__wrapped__
        _sampling_params = main.Server._sampling_params.__wrapped__
        _prompt_cache_hit_tokens = main.Server._prompt_cache_hit_tokens.__wrapped__
        _record_prompt_cache_prefixes = main.Server._record_prompt_cache_prefixes.__wrapped__

    monkeypatch.setitem(
        sys.modules,
        "vllm",
        type("FakeVllm", (), {"SamplingParams": FakeSamplingParams}),
    )

    server = FakeServer()

    async def close_after_first_data_chunk(prompt_ids):
        stream = main.Server._completion_stream(
            server,
            {
                "prompt": prompt_ids,
                "max_tokens": 1,
                "stream": True,
                "return_token_ids": True,
                "stream_options": {"include_usage": True},
            },
        )
        try:
            async for line in stream:
                payload = json.loads(line[len("data: ") :])
                assert "usage" not in payload
                return
        finally:
            await stream.aclose()
        raise AssertionError("stream did not emit a data chunk")

    async def final_usage_for(prompt_ids):
        stream = main.Server._completion_stream(
            server,
            {
                "prompt": prompt_ids,
                "max_tokens": 1,
                "stream": True,
                "return_token_ids": True,
                "stream_options": {"include_usage": True},
            },
        )
        try:
            async for line in stream:
                payload = json.loads(line[len("data: ") :])
                if "usage" in payload:
                    return payload["usage"]
        finally:
            await stream.aclose()
        raise AssertionError("stream did not emit final usage")

    prompt_ids = list(range(512))
    asyncio.run(close_after_first_data_chunk(prompt_ids))
    second = asyncio.run(final_usage_for(prompt_ids))

    assert second["prompt_tokens_details"]["cached_tokens"] == 496
    assert server.engine.prompts == [
        {"prompt_token_ids": prompt_ids},
        {"prompt_token_ids": prompt_ids},
    ]


def test_starter_overlays_local_vllm_source_onto_compiled_wheel():
    source = Path(main.__file__).read_text()

    assert main.VLLM_SOURCE == "third_party/vllm/vllm"
    assert main.VLLM_SOURCE_REMOTE == "/opt/vibesys-vllm-source/vllm"
    assert main.VLLM_SOURCE_PATCH_MANIFEST == "vllm_source_overlay.txt"
    assert ".add_local_dir(VLLM_SOURCE, remote_path=VLLM_SOURCE_REMOTE, copy=True)" in source
    assert ".add_local_file(" in source
    assert "VLLM_SOURCE_PATCH_MANIFEST," in source
    assert "remote_path=VLLM_SOURCE_PATCH_MANIFEST_REMOTE,\n        copy=True," in source
    assert ".run_commands(_VLLM_SOURCE_OVERLAY_COMMAND)" in source
    assert "for raw_line in manifest.read_text().splitlines()" in main._VLLM_SOURCE_OVERLAY_COMMAND
    assert "shutil.copy2(src, dst)" in main._VLLM_SOURCE_OVERLAY_COMMAND
    assert "shutil.copytree(source, target" not in main._VLLM_SOURCE_OVERLAY_COMMAND
    assert "PYTHONPATH" not in source
    assert Path("third_party/vllm/vllm").is_dir()
    assert Path(main.VLLM_SOURCE_PATCH_MANIFEST).is_file()


def test_gpu_debug_snapshot_parses_nvidia_smi(monkeypatch):
    def fake_run(*args, **kwargs):
        assert args[0][0] == "nvidia-smi"
        assert kwargs["timeout"] == 5
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                "2026/07/22 15:00:00.000, 0, NVIDIA H100 80GB HBM3, "
                "87, 42, 71320, 81559, 612.5, 700.0\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    snapshot = main._gpu_debug_snapshot()

    assert snapshot["available"] is True
    assert snapshot["gpus"][0]["utilization_gpu_pct"] == 87
    assert snapshot["gpus"][0]["memory_used_mib"] == 71320
    assert snapshot["gpus"][0]["power_draw_w"] == 612.5


def test_emitted_token_ids_drop_skipped_special_tokens():
    class FakeTokenizer:
        def encode(self, text, add_special_tokens=False):
            assert add_special_tokens is False
            return [1, 2, 3] if text == "abc" else []

    assert main._token_ids_for_emitted_text(FakeTokenizer(), "abc") == [1, 2, 3]
