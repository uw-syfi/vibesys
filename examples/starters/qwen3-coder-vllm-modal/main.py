import hashlib
import json
import math
import os
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import modal

APP_NAME = os.environ.get("VIBESYS_MODAL_APP_NAME", "vibesys-qwen3-coder-tracelab-vllm")
MODEL_ID = "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"
MODEL_VOLUME_NAME = "vibesys-model-qwen-qwen3-coder-30b-a3b-instruct-fp8"
MODEL_PATH = "/model"
ENABLE_PROMPT_TOKENS_DETAILS = True
VLLM_SOURCE = "third_party/vllm/vllm"
VLLM_SOURCE_REMOTE = "/opt/vibesys-vllm-source/vllm"
VLLM_SOURCE_PATCH_MANIFEST = "vllm_source_overlay.txt"
VLLM_SOURCE_PATCH_MANIFEST_REMOTE = "/opt/vibesys-vllm-source/overlay.txt"
VLLM_ATTENTION_BACKEND = "FLASH_ATTN"
VLLM_CUDAGRAPH_MODE = "FULL_AND_PIECEWISE"
VLLM_CUDAGRAPH_CAPTURE_SIZES = [1, 2, 4, 8, 16, 32, 64, 96, 128]
VLLM_MAX_NUM_SEQS = 128
VLLM_MAX_NUM_BATCHED_TOKENS = 32768
VLLM_MAX_MODEL_LEN = 262144
VLLM_MAX_COMPLETION_TOKENS = 4096
MODAL_FUNCTION_TIMEOUT = 3600
MODAL_STARTUP_TIMEOUT = 1800
_VLLM_SOURCE_OVERLAY_COMMAND = (
    "python - <<'PY'\n"
    "import shutil\n"
    "import site\n"
    "from pathlib import Path\n"
    f"source = Path({VLLM_SOURCE_REMOTE!r})\n"
    f"manifest = Path({VLLM_SOURCE_PATCH_MANIFEST_REMOTE!r})\n"
    "targets = [Path(p) / 'vllm' for p in site.getsitepackages()]\n"
    "target = next((p for p in targets if p.is_dir()), None)\n"
    "if target is None:\n"
    "    raise SystemExit('installed vLLM package directory not found')\n"
    "copied = 0\n"
    "for raw_line in manifest.read_text().splitlines():\n"
    "    line = raw_line.split('#', 1)[0].strip()\n"
    "    if not line:\n"
    "        continue\n"
    "    rel = Path(line)\n"
    "    if rel.is_absolute() or '..' in rel.parts:\n"
    "        raise SystemExit(f'invalid vLLM overlay path: {line}')\n"
    "    src = source / rel\n"
    "    dst = target / rel\n"
    "    if not src.is_file():\n"
    "        raise SystemExit(f'vLLM overlay file not found: {src}')\n"
    "    dst.parent.mkdir(parents=True, exist_ok=True)\n"
    "    shutil.copy2(src, dst)\n"
    "    copied += 1\n"
    "print(f'Overlayed {copied} manifest-listed local vLLM source file(s) onto {target}')\n"
    "PY"
)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi==0.139.2",
        "torch==2.11.0",
        "transformers==5.14.1",
        "vllm==0.22.1",
    )
    .add_local_dir(VLLM_SOURCE, remote_path=VLLM_SOURCE_REMOTE, copy=True)
    .add_local_file(
        VLLM_SOURCE_PATCH_MANIFEST,
        remote_path=VLLM_SOURCE_PATCH_MANIFEST_REMOTE,
        copy=True,
    )
    .run_commands(_VLLM_SOURCE_OVERLAY_COMMAND)
    .env(
        {
            "VLLM_ENABLE_V1_MULTIPROCESSING": "0",
            "VLLM_USE_FLASHINFER_SAMPLER": "0",
            "VLLM_USE_DEEP_GEMM": "0",
            "VLLM_MOE_USE_DEEP_GEMM": "0",
            "VLLM_DEEP_GEMM_WARMUP": "skip",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
)

model_volume = modal.Volume.from_name(MODEL_VOLUME_NAME)
app = modal.App(APP_NAME)


def _now() -> int:
    return int(time.time())


def _request_id(prefix: str = "cmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _normalize_stop(stop: Any) -> str | list[str] | None:
    if stop is None:
        return None
    if isinstance(stop, str):
        return stop
    if isinstance(stop, list) and all(isinstance(item, str) for item in stop):
        return stop
    return None


def _usage(
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int | None = None,
) -> dict[str, Any]:
    usage = {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(prompt_tokens + completion_tokens),
    }
    if ENABLE_PROMPT_TOKENS_DETAILS:
        usage["prompt_tokens_details"] = {"cached_tokens": int(cached_tokens or 0)}
    return usage


def _cache_block_size(engine_args: Any | None) -> int:
    cache_config = getattr(engine_args, "cache_config", None)
    block_size = getattr(cache_config, "block_size", None)
    if isinstance(block_size, int) and block_size > 0:
        return block_size
    return 16


def _full_prefix_cache_len(prompt_token_ids: list[int], block_size: int) -> int:
    if block_size <= 0 or len(prompt_token_ids) <= 1:
        return 0
    return ((len(prompt_token_ids) - 1) // block_size) * block_size


def _prompt_prefix_cache_keys(
    prompt_token_ids: list[int],
    block_size: int,
) -> list[tuple[int, bytes]]:
    max_len = _full_prefix_cache_len(prompt_token_ids, block_size)
    if max_len == 0:
        return []
    digest = hashlib.blake2b(digest_size=16)
    keys: list[tuple[int, bytes]] = []
    for idx, token_id in enumerate(prompt_token_ids[:max_len], start=1):
        digest.update(int(token_id).to_bytes(8, byteorder="little", signed=True))
        if idx % block_size == 0:
            keys.append((idx, digest.digest()))
    return keys


def _coerce_cached_tokens(value: Any) -> int:
    if value is None:
        return 0
    try:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return 0
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _cached_tokens_from_output(output: Any | None, fallback: int = 0) -> int:
    if output is None:
        return max(0, int(fallback))
    candidates = [
        getattr(output, "num_cached_tokens", None),
        getattr(getattr(output, "metrics", None), "num_cached_tokens", None),
        getattr(getattr(output, "prefill_stats", None), "num_cached_tokens", None),
    ]
    return max([max(0, int(fallback)), *[_coerce_cached_tokens(item) for item in candidates]])


def _coerce_messages(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    out: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("each message must be an object")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            raise ValueError("each message must contain string role and content")
        out.append({"role": role, "content": content})
    return out


def _choice_token_ids(choice: Any) -> list[int]:
    return [int(tok) for tok in (getattr(choice, "token_ids", None) or [])]


def _token_ids_for_emitted_text(tokenizer: Any, text: str) -> list[int]:
    if not text:
        return []
    # vLLM may include sampled EOS/special IDs in choice.token_ids while
    # choice.text has already applied skip_special_tokens and stop removal.
    return [int(tok) for tok in tokenizer.encode(text, add_special_tokens=False)]


def _finish_reason(choice: Any) -> str:
    reason = getattr(choice, "finish_reason", None)
    if reason == "length":
        return "length"
    return "stop"


def _vllm_engine_kwargs(attention_backend: Any | None = None) -> dict[str, Any]:
    compilation_config = {
        "mode": "VLLM_COMPILE",
        "backend": "inductor",
        "cudagraph_mode": VLLM_CUDAGRAPH_MODE,
        "cudagraph_capture_sizes": VLLM_CUDAGRAPH_CAPTURE_SIZES,
        "compile_sizes": ["cudagraph_capture_sizes"],
        "cudagraph_num_of_warmups": 1,
    }
    kwargs: dict[str, Any] = {
        "model": MODEL_PATH,
        "tokenizer": MODEL_PATH,
        "served_model_name": MODEL_ID,
        "trust_remote_code": True,
        "dtype": "auto",
        "kv_cache_dtype": "auto",
        "quantization": None,
        "load_format": "auto",
        "max_model_len": VLLM_MAX_MODEL_LEN,
        "enable_prefix_caching": True,
        "prefix_caching_hash_algo": "sha256",
        "gpu_memory_utilization": 0.92,
        "max_num_seqs": VLLM_MAX_NUM_SEQS,
        "max_num_batched_tokens": VLLM_MAX_NUM_BATCHED_TOKENS,
        "enable_chunked_prefill": True,
        "disable_log_stats": False,
        "enforce_eager": False,
        "enable_log_requests": False,
        "linear_backend": "triton",
        "compilation_config": compilation_config,
        "cudagraph_metrics": True,
        "kv_cache_metrics": True,
    }
    if attention_backend is not None:
        kwargs["attention_backend"] = attention_backend
    return kwargs


def _serving_floor_metadata(engine_args: Any | None = None) -> dict[str, Any]:
    compilation = getattr(engine_args, "compilation_config", None)
    return {
        "modal_app_name": APP_NAME,
        "model_volume_name": MODEL_VOLUME_NAME,
        "model_path": MODEL_PATH,
        "engine": "vllm",
        "continuous_batching": {
            "enabled": True,
            "max_num_seqs": VLLM_MAX_NUM_SEQS,
            "max_num_batched_tokens": VLLM_MAX_NUM_BATCHED_TOKENS,
        },
        "prefix_caching": {"enabled": True, "hash_algo": "sha256"},
        "chunked_prefill": {"enabled": True},
        "attention_backend": str(
            getattr(engine_args, "attention_backend", None) or VLLM_ATTENTION_BACKEND
        ),
        "cuda_graph": {
            "configured": True,
            "mode": str(getattr(compilation, "cudagraph_mode", VLLM_CUDAGRAPH_MODE)),
            "capture_sizes": list(
                getattr(compilation, "cudagraph_capture_sizes", None)
                or VLLM_CUDAGRAPH_CAPTURE_SIZES
            ),
            "compile_sizes": list(
                getattr(compilation, "compile_sizes", None)
                or ["cudagraph_capture_sizes"]
            ),
            "enforce_eager": False,
        },
        "vllm_source": VLLM_SOURCE,
    }


def _gpu_debug_snapshot() -> dict[str, Any]:
    query = (
        "timestamp,index,name,utilization.gpu,utilization.memory,memory.used,"
        "memory.total,power.draw,power.limit"
    )
    result = subprocess.run(
        [
            "nvidia-smi",
            f"--query-gpu={query}",
            "--format=csv,noheader,nounits",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        return {"available": False, "error": result.stderr.strip() or result.stdout.strip()}
    rows: list[dict[str, Any]] = []
    fields = [
        "timestamp",
        "index",
        "name",
        "utilization_gpu_pct",
        "utilization_memory_pct",
        "memory_used_mib",
        "memory_total_mib",
        "power_draw_w",
        "power_limit_w",
    ]
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != len(fields):
            continue
        row = dict(zip(fields, parts, strict=True))
        for key in fields[1:]:
            try:
                row[key] = float(row[key]) if "." in str(row[key]) else int(row[key])
            except (TypeError, ValueError):
                pass
        rows.append(row)
    return {"available": True, "gpus": rows}


def _summarize(prof: Any, num_iters: int) -> dict[str, Any]:
    import torch
    from torch.autograd import DeviceType

    totals = prof.key_averages()
    events: list[dict[str, Any]] = []
    total_cuda = 0.0
    total_cpu = 0.0
    for ev in totals:
        cuda_us = float(getattr(ev, "device_time_total", 0.0) or 0.0)
        cpu_us = float(getattr(ev, "cpu_time_total", 0.0) or 0.0)
        self_cuda = float(getattr(ev, "self_device_time_total", 0.0) or 0.0)
        self_cpu = float(getattr(ev, "self_cpu_time_total", 0.0) or 0.0)
        name = ev.key
        if ev.device_type == DeviceType.CUDA or "cuda" in name.lower() or (
            cuda_us > 0 and cpu_us < cuda_us / 4
        ):
            category = "kernel"
        elif name.startswith("aten::") or name.startswith("torch::"):
            category = "operator"
        elif any(term in name.lower() for term in ("memcpy", "memset", "malloc", "free")):
            category = "memory"
        else:
            category = "cpu"
        events.append(
            {
                "name": name,
                "category": category,
                "cpu_time_us": cpu_us,
                "cuda_time_us": cuda_us,
                "self_cpu_time_us": self_cpu,
                "self_cuda_time_us": self_cuda,
                "count": int(ev.count),
            }
        )
        total_cuda += self_cuda
        total_cpu += self_cpu
    return {
        "version": 1,
        "total_cuda_time_us": total_cuda,
        "total_cpu_time_us": total_cpu,
        "num_events": len(events),
        "events": events,
        "num_iters": num_iters,
        "device": "cuda" if torch.cuda.is_available() else "cpu",
    }


@app.cls(
    image=image,
    gpu="H100",
    volumes={MODEL_PATH: model_volume},
    max_containers=1,
    scaledown_window=120,
    timeout=MODAL_FUNCTION_TIMEOUT,
    startup_timeout=MODAL_STARTUP_TIMEOUT,
)
@modal.concurrent(max_inputs=32, target_inputs=8)
class Server:
    @modal.enter()
    async def load(self) -> None:
        from transformers import AutoTokenizer
        from vllm import AsyncLLMEngine
        from vllm.engine.arg_utils import AsyncEngineArgs
        from vllm.v1.attention.backends.registry import AttentionBackendEnum

        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_PATH,
            trust_remote_code=True,
            local_files_only=True,
        )
        self.engine_args = AsyncEngineArgs(
            **_vllm_engine_kwargs(attention_backend=AttentionBackendEnum.FLASH_ATTN)
        )
        self.serving_floor = _serving_floor_metadata(self.engine_args)
        self.cache_block_size = _cache_block_size(self.engine_args)
        self._completed_prompt_prefixes: set[tuple[int, bytes]] = set()
        self.engine = AsyncLLMEngine.from_engine_args(self.engine_args)

    def _sampling_params(self, body: dict[str, Any]):
        from vllm import SamplingParams

        temperature = float(body.get("temperature", 1.0))
        top_p = float(body.get("top_p", 1.0))
        max_tokens = int(body.get("max_tokens", 256))
        return SamplingParams(
            temperature=temperature,
            top_p=top_p,
            max_tokens=max(1, min(max_tokens, VLLM_MAX_COMPLETION_TOKENS)),
            stop=_normalize_stop(body.get("stop")),
            ignore_eos=bool(body.get("ignore_eos", False)),
            skip_special_tokens=True,
            include_stop_str_in_output=False,
        )

    def _prompt_arg(self, prompt: Any) -> tuple[Any, list[int]]:
        if isinstance(prompt, str):
            token_ids = self.tokenizer.encode(prompt, add_special_tokens=False)
            return prompt, [int(tok) for tok in token_ids]
        if isinstance(prompt, list) and all(isinstance(tok, int) for tok in prompt):
            ids = [int(tok) for tok in prompt]
            return {"prompt_token_ids": ids}, ids
        raise ValueError("prompt must be a string or a list of integer token IDs")

    def _chat_prompt_arg(self, messages: Any) -> tuple[str, list[int]]:
        normalized = _coerce_messages(messages)
        if getattr(self.tokenizer, "chat_template", None):
            prompt = self.tokenizer.apply_chat_template(
                normalized,
                tokenize=False,
                add_generation_prompt=True,
            )
        else:
            parts = [f"{message['role']}: {message['content']}" for message in normalized]
            parts.append("assistant:")
            prompt = "\n".join(parts)
        token_ids = self.tokenizer.encode(prompt, add_special_tokens=False)
        return prompt, [int(tok) for tok in token_ids]

    def _prompt_cache_hit_tokens(self, prompt_token_ids: list[int]) -> int:
        keys = _prompt_prefix_cache_keys(prompt_token_ids, self.cache_block_size)
        for prefix_len, cache_key in reversed(keys):
            if (prefix_len, cache_key) in self._completed_prompt_prefixes:
                return prefix_len
        return 0

    def _record_prompt_cache_prefixes(self, prompt_token_ids: list[int]) -> None:
        self._completed_prompt_prefixes.update(
            _prompt_prefix_cache_keys(prompt_token_ids, self.cache_block_size)
        )

    async def _run_completion(self, body: dict[str, Any]) -> dict[str, Any]:
        prompt, prompt_token_ids = self._prompt_arg(body.get("prompt"))
        prompt_tokens = len(prompt_token_ids)
        fallback_cached_tokens = self._prompt_cache_hit_tokens(prompt_token_ids)
        sampling_params = self._sampling_params(body)
        request_id = _request_id("cmpl")
        created = _now()
        final_output = None

        async for output in self.engine.generate(prompt, sampling_params, request_id):
            final_output = output

        if final_output is None or not final_output.outputs:
            text = ""
            token_ids: list[int] = []
            finish_reason = "stop"
            cached_tokens = fallback_cached_tokens
        else:
            choice = final_output.outputs[0]
            text = choice.text
            token_ids = _token_ids_for_emitted_text(self.tokenizer, text)
            finish_reason = _finish_reason(choice)
            cached_tokens = _cached_tokens_from_output(
                final_output,
                fallback=fallback_cached_tokens,
            )

        self._record_prompt_cache_prefixes(prompt_token_ids)

        choice_payload: dict[str, Any] = {
            "text": text,
            "index": 0,
            "finish_reason": finish_reason,
        }
        if body.get("return_token_ids"):
            choice_payload["token_ids"] = token_ids
            choice_payload["logprobs"] = {"token_ids": token_ids}

        return {
            "id": request_id,
            "object": "text_completion",
            "created": created,
            "model": MODEL_ID,
            "choices": [choice_payload],
            "usage": _usage(prompt_tokens, len(token_ids), cached_tokens),
        }

    async def _run_chat_completion(self, body: dict[str, Any]) -> dict[str, Any]:
        prompt, prompt_token_ids = self._chat_prompt_arg(body.get("messages"))
        prompt_tokens = len(prompt_token_ids)
        fallback_cached_tokens = self._prompt_cache_hit_tokens(prompt_token_ids)
        sampling_params = self._sampling_params(body)
        request_id = _request_id("chatcmpl")
        created = _now()
        final_output = None

        async for output in self.engine.generate(prompt, sampling_params, request_id):
            final_output = output

        if final_output is None or not final_output.outputs:
            text = ""
            token_ids: list[int] = []
            finish_reason = "stop"
            cached_tokens = fallback_cached_tokens
        else:
            choice = final_output.outputs[0]
            text = choice.text
            token_ids = _token_ids_for_emitted_text(self.tokenizer, text)
            finish_reason = _finish_reason(choice)
            cached_tokens = _cached_tokens_from_output(
                final_output,
                fallback=fallback_cached_tokens,
            )

        self._record_prompt_cache_prefixes(prompt_token_ids)

        return {
            "id": request_id,
            "object": "chat.completion",
            "created": created,
            "model": MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": _usage(prompt_tokens, len(token_ids), cached_tokens),
        }

    async def _completion_stream(self, body: dict[str, Any]) -> AsyncIterator[str]:
        prompt, prompt_token_ids = self._prompt_arg(body.get("prompt"))
        prompt_tokens = len(prompt_token_ids)
        fallback_cached_tokens = self._prompt_cache_hit_tokens(prompt_token_ids)
        sampling_params = self._sampling_params(body)
        request_id = _request_id("cmpl")
        created = _now()
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        return_token_ids = bool(body.get("return_token_ids"))
        emitted_text_len = 0
        emitted_token_len = 0
        final_completion_tokens = 0
        cached_tokens = fallback_cached_tokens
        finish_reason = "stop"

        async for output in self.engine.generate(prompt, sampling_params, request_id):
            if not output.outputs:
                continue
            choice = output.outputs[0]
            text = choice.text or ""
            token_ids = _token_ids_for_emitted_text(self.tokenizer, text)
            delta_text = text[emitted_text_len:] if len(text) >= emitted_text_len else ""
            delta_token_ids = (
                token_ids[emitted_token_len:]
                if len(token_ids) >= emitted_token_len
                else []
            )
            emitted_text_len = len(text)
            emitted_token_len = len(token_ids)
            final_completion_tokens = len(token_ids)
            cached_tokens = _cached_tokens_from_output(
                output,
                fallback=max(cached_tokens, fallback_cached_tokens),
            )
            if getattr(choice, "finish_reason", None):
                finish_reason = _finish_reason(choice)

            if delta_text or delta_token_ids:
                choice_payload: dict[str, Any] = {
                    "text": delta_text,
                    "index": 0,
                    "finish_reason": None,
                }
                if return_token_ids:
                    choice_payload["token_ids"] = delta_token_ids
                    choice_payload["logprobs"] = {"token_ids": delta_token_ids}
                chunk = {
                    "id": request_id,
                    "object": "text_completion",
                    "created": created,
                    "model": MODEL_ID,
                    "choices": [choice_payload],
                }
                yield f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"

        final_chunk: dict[str, Any] = {
            "id": request_id,
            "object": "text_completion",
            "created": created,
            "model": MODEL_ID,
            "choices": [{"text": "", "index": 0, "finish_reason": finish_reason}],
        }
        if include_usage:
            final_chunk["usage"] = _usage(prompt_tokens, final_completion_tokens, cached_tokens)
        yield f"data: {json.dumps(final_chunk, separators=(',', ':'))}\n\n"
        yield "data: [DONE]\n\n"
        self._record_prompt_cache_prefixes(prompt_token_ids)

    async def _chat_completion_stream(self, body: dict[str, Any]) -> AsyncIterator[str]:
        prompt, prompt_token_ids = self._chat_prompt_arg(body.get("messages"))
        prompt_tokens = len(prompt_token_ids)
        fallback_cached_tokens = self._prompt_cache_hit_tokens(prompt_token_ids)
        sampling_params = self._sampling_params(body)
        request_id = _request_id("chatcmpl")
        created = _now()
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        emitted_text_len = 0
        final_completion_tokens = 0
        cached_tokens = fallback_cached_tokens
        finish_reason = "stop"

        async for output in self.engine.generate(prompt, sampling_params, request_id):
            if not output.outputs:
                continue
            choice = output.outputs[0]
            text = choice.text or ""
            token_ids = _token_ids_for_emitted_text(self.tokenizer, text)
            delta_text = text[emitted_text_len:]
            emitted_text_len = len(text)
            final_completion_tokens = len(token_ids)
            cached_tokens = _cached_tokens_from_output(
                output,
                fallback=max(cached_tokens, fallback_cached_tokens),
            )
            if getattr(choice, "finish_reason", None):
                finish_reason = _finish_reason(choice)

            if delta_text:
                chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": delta_text},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"

        final_chunk: dict[str, Any] = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
        if include_usage:
            final_chunk["usage"] = _usage(prompt_tokens, final_completion_tokens, cached_tokens)
        yield f"data: {json.dumps(final_chunk, separators=(',', ':'))}\n\n"
        yield "data: [DONE]\n\n"
        self._record_prompt_cache_prefixes(prompt_token_ids)

    @modal.method()
    async def generate(self, prompt: str, max_tokens: int = 32, temperature: float = 0.0) -> dict[str, Any]:
        return await self._run_completion(
            {
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }
        )

    @modal.asgi_app(label=f"{APP_NAME}-web")
    def web(self):
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse, StreamingResponse

        web_app = FastAPI()

        @web_app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @web_app.get("/debug/gpu")
        async def debug_gpu() -> dict[str, Any]:
            return _gpu_debug_snapshot()

        @web_app.get("/v1/models")
        async def models() -> dict[str, Any]:
            return {
                "object": "list",
                "data": [
                    {
                        "id": MODEL_ID,
                        "object": "model",
                        "created": 0,
                        "owned_by": "local",
                    }
                ],
            }

        @web_app.post("/v1/completions")
        async def completions(request: Request):
            body = await request.json()
            if "prompt" not in body:
                raise HTTPException(status_code=400, detail="prompt is required")
            try:
                if bool(body.get("stream", False)):
                    return StreamingResponse(
                        self._completion_stream(body),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )
                return JSONResponse(await self._run_completion(body))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @web_app.post("/v1/chat/completions")
        async def chat_completions(request: Request):
            body = await request.json()
            if "messages" not in body:
                raise HTTPException(status_code=400, detail="messages is required")
            try:
                if bool(body.get("stream", False)):
                    return StreamingResponse(
                        self._chat_completion_stream(body),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )
                return JSONResponse(await self._run_chat_completion(body))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        return web_app


@app.function(
    image=image,
    gpu="H100",
    volumes={MODEL_PATH: model_volume},
    timeout=MODAL_FUNCTION_TIMEOUT,
    startup_timeout=MODAL_STARTUP_TIMEOUT,
)
def profile_remote(num_iters: int, max_tokens: int, prompt: str) -> dict[str, Any]:
    import torch
    from torch.profiler import ProfilerActivity, profile, record_function, schedule
    from vllm import LLM, SamplingParams
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.v1.attention.backends.registry import AttentionBackendEnum

    def run_once(llm: Any, tokens: int) -> None:
        params = SamplingParams(
            temperature=0.0,
            max_tokens=max(1, tokens),
            skip_special_tokens=True,
            include_stop_str_in_output=False,
        )
        llm.generate([prompt], params, use_tqdm=False)

    def run_profile() -> tuple[dict[str, Any], float]:
        engine_args = AsyncEngineArgs(
            **_vllm_engine_kwargs(attention_backend=AttentionBackendEnum.FLASH_ATTN)
        )
        llm_kwargs = _vllm_engine_kwargs(attention_backend=AttentionBackendEnum.FLASH_ATTN)
        llm_kwargs.pop("served_model_name", None)
        llm_kwargs.pop("enable_log_requests", None)
        llm = LLM(**llm_kwargs)
        run_once(llm, min(4, max(1, max_tokens)))
        torch.cuda.synchronize()

        active_iters = max(1, int(num_iters))
        wall_start = time.perf_counter()
        with profile(
            activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
            schedule=schedule(wait=0, warmup=0, active=active_iters, repeat=1),
            record_shapes=True,
            with_stack=False,
        ) as prof:
            for _ in range(active_iters):
                with record_function("vllm_generate"):
                    run_once(llm, max_tokens)
                torch.cuda.synchronize()
                prof.step()
        summary = _summarize(prof, active_iters)
        summary["serving_floor"] = _serving_floor_metadata(engine_args)
        summary["attention_backend"] = str(AttentionBackendEnum.FLASH_ATTN)
        summary["cuda_graph_configured"] = True
        summary["cuda_graph_capture_sizes"] = VLLM_CUDAGRAPH_CAPTURE_SIZES
        summary["cuda_graph_mode"] = VLLM_CUDAGRAPH_MODE
        summary["cuda_graph_observed_events"] = [
            event
            for event in summary["events"]
            if "cudagraph" in event["name"].lower()
            or "cuda graph" in event["name"].lower()
            or "cudaGraph" in event["name"]
        ]
        wall_time = time.perf_counter() - wall_start
        shutdown = getattr(llm.llm_engine, "shutdown", None)
        if callable(shutdown):
            shutdown()
        return summary, wall_time

    summary, wall_time = run_profile()
    summary["captured_at"] = datetime.now(UTC).isoformat()
    summary["mode"] = "model"
    summary["device"] = "cuda"
    summary["dtype"] = "auto"
    summary["wall_time_sec"] = wall_time
    return summary


@app.local_entrypoint()
def modal_profile(
    output: str = "/workspace/prof.json",
    num_iters: int = 20,
    max_tokens: int = 32,
    prompt: str = "The capital of France is",
) -> None:
    result = profile_remote.remote(num_iters, max_tokens, prompt)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2))
