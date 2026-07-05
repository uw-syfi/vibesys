---
name: vs-init
description: Create or update a VibeServe LLM model-serving example bundle for a new Hugging Face model, hardware target, and optimization workload. Use when the user wants to add an example under examples/model-serving, scaffold reference/accuracy_checker/benchmark inputs, adapt existing checker and HTTP benchmark scripts, or start an optimization run from a natural-language workload goal without adding new reusable framework code.
---

# VS Init

## Goal

Create a new `examples/model-serving/<name>/` input bundle using today's VibeServe conventions. Prefer copying and adapting the closest existing example over inventing shared infrastructure.

The user should only need to provide:

- Hugging Face model id, and revision if important.
- Hardware target, such as H100, A100, Trainium, MacBook/MLX, CPU.
- Natural-language workload / optimization goal, for example: "maximize p95 latency for 128-token chat completions at 8 req/s" or "measure prefix caching for long shared prompts."

If the workload goal is missing or vague, ask the user to describe it in plain English before writing files.
Always infer the model/workload modality and a bundle slug, then ask the user to confirm both before creating files.

## Workflow

1. Inspect existing examples with `find examples/model-serving -maxdepth 2 -type d | sort`.
2. Identify the workload's input-to-output modality. Do not assume the Hugging Face model id is sufficient. Use model id, tags/config, README/model card if available, and the user's natural-language goal as clues.
3. Propose:
   - Inferred modality/API, such as "text/chat -> text over OpenAI completions".
   - Source example to copy.
   - Bundle slug, such as `qwen3-8b-h100-json` or `llama-3-8b-a100-chat`.
4. Ask the user to confirm or correct both the modality/API and slug before creating files. Keep the question concise:
   - "I infer this is `<input> -> <output>` via `<API>`, and I would create `examples/model-serving/<slug>/`. Is that right?"
5. Pick the nearest source example by input-to-output modality and public API first; use hardware as a secondary tie-breaker. The accuracy checker and benchmark are usually tied more tightly to the modality/workload contract than to the accelerator.
   - Text prompt/chat -> text completion, OpenAI completions/chat API: copy from `Llama-3-8B`.
   - Text prompt/chat -> text completion on Trainium: copy from `Llama-3-8B-trn2` only when Trainium-specific setup matters; otherwise start from the generic text example and adjust hardware notes.
   - Text/code input -> edited code output with predicted-output requests: copy from `qwen3-32b-code-edit`.
   - Text prompt/schema -> constrained JSON text output: copy from `Llama-3.1-8B-Instruct-MLX-8bit`.
   - Text prompt with long shared prefixes -> text completion with prefix-cache-sensitive benchmark: copy from `olmo-hybrid-prefix-caching`.
   - Audio stream or WAV input -> transcript text: copy from `moonshine-streaming`.
   - Text prompt -> image bytes/base64: copy from `show-o2-1.5B-HQ*`.
   - If no example matches the modality, ask the user which input/output contract to measure and create the smallest checker/benchmark by adapting the closest transport pattern (HTTP, WebSocket, or local `VibeServeModel`).
6. Create `examples/model-serving/<slug>/` with the same layout:
   - `OBJECTIVE.md`
   - `README.md`
   - `requirements.txt`
   - `config.json` if the source example has one
   - `reference/README.md`, `reference/meta.json`, `reference/config.json` if applicable, `reference/reference.py`
   - `accuracy_checker/README.md`, `accuracy_checker/checker.py`
   - `benchmark/README.md`, `benchmark/benchmark.py`
7. Preserve the existing VibeServe contract:
   - Reference path is passed with `--ref examples/model-serving/<slug>/reference`.
   - Accuracy checker path is passed with `--acc-checker examples/model-serving/<slug>/accuracy_checker`.
   - Benchmark path is passed with `--bench examples/model-serving/<slug>/benchmark`.
   - Checkers should be executable as `python checker.py`.
   - Benchmarks should be executable as `python benchmark.py --url http://localhost:8000 ...`.
8. Do not add a new shared evaluator library unless the user explicitly asks. This skill is for practical example setup, not refactoring.

## Modality Inference

Treat model modality as a hypothesis, not a fact, until the user confirms it.

Useful clues:

- `AutoModelForCausalLM`, `text-generation`, `chat`, `instruct`, `code`: usually text -> text.
- `response_format`, JSON schema, grammar/constrained decoding goal: text/schema -> structured text.
- `image-to-text`, `vision-language`, `vl`, `vllava`, `qwen-vl`: image+text -> text, which may need a new closest-example adaptation because current examples are mostly text-only and text-to-image.
- `text-to-image`, diffusion, `StableDiffusion`, `Show-o`: text -> image.
- `automatic-speech-recognition`, `speech-to-text`, `audio`, `whisper`, `moonshine`: audio -> text.
- `text-to-speech`, `tts`: text -> audio, currently no direct model-serving source example; ask before adapting.

Ambiguous cases to clarify:

- Multimodal generative models that support more than one route, such as text -> text and image+text -> text.
- Base models where the user's workload is specialized, such as code editing, JSON generation, long-context caching, or chat serving.
- Model ids that name a framework or hardware format rather than task semantics, such as MLX, GGUF, AWQ, GPTQ, or Neuron.

Even when the modality seems obvious, ask for confirmation before writing files. If it is ambiguous, include the uncertainty in the confirmation question: "I think this is probably text -> text, but it may be image+text -> text. Which modality should this benchmark exercise?"

## Slug Naming

Generate a concise lowercase slug with letters, digits, and hyphens only. Prefer:

```text
<model-family>-<size>-<hardware-or-workload>
```

Examples:

- `qwen3-8b-h100-chat`
- `llama-3-8b-trn2`
- `qwen3-32b-code-edit`
- `olmo-prefix-caching`

Ask the user to confirm the slug before creating `examples/model-serving/<slug>/`. If the user supplies a name, normalize it to hyphen-case and confirm it unless the request explicitly says to use that exact name.

## Objective

Write `OBJECTIVE.md` from the user's natural-language goal. Include:

- Model name and serving modality.
- Hardware target.
- Primary metric and whether higher or lower is better.
- Required API shape, usually OpenAI-compatible `/v1/completions` and optionally `/v1/chat/completions`.
- Accuracy requirement, usually "must pass the accuracy checker."
- Notes about allowed implementation approaches and disallowed shortcuts.

Keep it concrete enough for an optimizer to know what to improve. If the goal says "benchmark X", name X as the headline metric rather than relying on profiler-only timings.

## Reference

For normal Hugging Face text-generation models, make `reference/meta.json` the source of truth:

```json
{
  "model_id": "org/model-name",
  "revision": null,
  "task": "text-generation"
}
```

Use `revision` when the user gives one or reproducibility matters. VibeServe already knows how to use `meta.json` to materialize model weights in `reference/model`.

For generic causal LMs, adapt `reference/reference.py` from `Llama-3-8B` unless the model requires custom code. Use `AutoTokenizer` and `AutoModelForCausalLM`. Set `trust_remote_code=True` only when the model requires it or the user explicitly accepts it.

For nonstandard models, keep `reference/reference.py` as an explanatory, runnable reference implementation matching the source example's pattern.

## Accuracy Checker

For a new HF causal LM, start from `examples/model-serving/Llama-3-8B/accuracy_checker/checker.py`.

Adapt:

- Model path default (`--model-dir ../model` is usually fine once copied into the bundle).
- Device and dtype defaults for the hardware target.
- Prompt suite if the workload is specialized.
- Chat-template behavior if the model is instruct/chat tuned.
- Comparator strictness only when justified.

Default text-matching behavior should be strict greedy matching against Hugging Face outputs:

- Use deterministic generation: `do_sample=False`, temperature 0 or unset.
- Compare generated token ids first.
- Print decoded reference/custom text and first differing token when failing.
- Exit 0 only when all required cases pass.

For workload-specific checks, add targeted cases instead of weakening the checker. Examples:

- Prefix caching: include long shared prefixes and divergent suffixes.
- Code editing: include buggy code and compare against gold fixes or similarity thresholds.
- JSON/constrained decoding: validate JSON schema and include randomized sentinel text to catch prompt-ignoring shortcuts.

## Benchmark

For a new OpenAI-compatible text-generation server, start from `examples/model-serving/Llama-3-8B/benchmark/benchmark.py`.

Keep these reusable behaviors unless the workload says otherwise:

- `--url`, `--endpoint`, `--rate`, `--duration`, `--num-requests`, `--max-tokens`, `--temperature`, `--prompt-len`, `--seed`, `--output-json`.
- Streaming SSE request handling.
- TTFT, TPOT, total latency, request throughput, and output token throughput.
- Poisson arrivals for open-loop load.
- Structured JSON output.

Adapt the prompt pool and metric focus to the natural-language goal:

- Throughput workloads: report token throughput as headline.
- Latency workloads: report p50/p95/p99 total latency or TTFT as headline.
- Prefix-cache workloads: include repeated shared-prefix prompts.
- Long-context workloads: add synthetic or dataset-backed long prompts.
- Code-edit/predicted-output workloads: copy from `qwen3-32b-code-edit` and keep the `prediction` field behavior.

Do not make benchmark success depend on hidden implementation details. Measure end-to-end behavior through the public API unless the user specifically asks for a local/in-process benchmark.

## README And Requirements

Write a short `README.md` with the exact paths to use:

```text
Use:
- --ref examples/model-serving/<slug>/reference
- --acc-checker examples/model-serving/<slug>/accuracy_checker
- --bench examples/model-serving/<slug>/benchmark
```

Update `requirements.txt` from the copied example. Include only dependencies the checker/reference/benchmark need, such as `torch`, `transformers`, `accelerate`, `httpx`, `datasets`, `jsonschema`, `soundfile`, or `websockets`.

## Validation

After creating or editing the example:

1. Run syntax checks on changed Python files with `python3 -m py_compile ...` when dependencies are not installed.
2. Run lightweight `--help` checks for `accuracy_checker/checker.py` and `benchmark/benchmark.py` if imports allow it.
3. Verify `reference/meta.json` is valid JSON.
4. Verify the final layout with `find examples/model-serving/<slug> -maxdepth 3 -type f | sort`.
5. Do not download large model weights or run GPU-heavy checks unless the user asks.

## Handoff

End with:

- The new bundle path.
- The source example copied/adapted.
- The optimization goal captured in `OBJECTIVE.md`.
- The exact `vibeserve` command to start optimizing, using the bundle's `--ref`, `--acc-checker`, and `--bench` paths.
