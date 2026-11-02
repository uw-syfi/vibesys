# Flint agentic serving on AMD

Three selectable tasks share one candidate project:

- **`mi2104x`**: four AMD MI210 GPUs, 64 GB HBM per GPU.
- **`mi300x`**: one AMD MI300X GPU, 192 GB HBM.
- **`mi2101x-int4`**: one AMD MI210 GPU, 64 GB HBM, official Q4_1 weights.

The two BF16 tasks target the BF16 `RecursiveSI/flint` checkpoint at revision
`2a247be0c0aec895ccb0ac7ca92229ec6df37288`, replay the same pinned AgentX
coding corpus, and optimize the same throughput/TTFT/TPOT Pareto frontier.
Select the task when creating a run from this example. The registry discovers
all task names; they are hardware and precision variants over one candidate codebase.

This is a **from-scratch port-and-optimize task**. No candidate engine or
validated AMD serving baseline is supplied. Implement a short-request service
first, pass the numerical gate, then add long-context streaming and optimize
AgentX. Unsupported ROCm kernels are part of the task. Transformers is available
for weight loading and reference operations; it is not evidence of an optimized
AMD serving implementation. Upstream engine implementations can inform the
port, but the candidate must own its serving path rather than proxy another
server.

The hardware objectives are in
[`mi2104x/OBJECTIVE.md`](.vibesys/tasks/mi2104x/OBJECTIVE.md) and
[`mi300x/OBJECTIVE.md`](.vibesys/tasks/mi300x/OBJECTIVE.md). The shared BF16
[accuracy checker](.vibesys/shared/checker.py) compares the live service with a
pinned Transformers eager reference, including Flint's learned attention sinks.
The trusted files live under `.vibesys/`; candidate code belongs outside it.

## BF16 setup and shared service interfaces

Use Linux x86-64 with ROCm 6.4. The project's `pyproject.toml` pins the
Transformers implementation and ROCm PyTorch utilities. Download the pinned
checkpoint into the normal Hugging Face cache or the operator-provided model
mount. Weight metadata is task-scoped so each remote task can provision it.
The BF16 weights occupy roughly 80 GB; cache and workspace are additional.

The BF16 numerical gate loads its reference on **CPU**, separately from candidate
GPU allocations. Provision **at least 100 GiB of available host RAM** for
reference loading and scoring, and leave room for the candidate's host work.
The gate is deliberately slower than a cached golden: it uses the candidate's
own continuation and fresh prompt content, so no GPU-specific golden or fixed
answer table is needed. It does not reserve a second GPU or evict candidate KV
state to load a reference. Full-size gate runtime and memory have not yet been
measured on either cluster partition.

The service must expose:

- `/v1/chat/completions` with streaming, `ignore_eos`, final token usage, and
  the pinned chat template. Preserve reasoning and tool history.
- `/v1/completions` accepting token-ID prompts for numerical verification,
  `temperature=0`, `ignore_eos`, `return_token_ids`, and generated-token logprobs.
- Nonstreamed chat with `return_token_ids` and standard chat `logprobs.content`,
  so the gate can compare chat against the exact template's raw completion.

Both response forms must return `choices[0].token_ids` for requested token IDs;
these include all generated tokens, including reasoning and special tokens.
The checker validates lengths, vocabulary bounds, finite probabilities, usage,
repeatable greedy IDs, and numerical agreement with the trusted reference.
It covers short inputs and lengths immediately around the 513-token local
attention window. Greedy decoding here is a numerical probe, not a claim about
Flint's recommended agent sampling settings. See the checker for error budgets.

## Measurement

The shared evaluator is [`agentx-v1`](../../../resources/evaluators/agentx/README.md).
All tasks use four concurrent session trees, native 262K context, seed 42,
a 900-second profiling window, and the same warmup and replay recipe as the
Qwen AgentX example. Each task gets an independent frontier; do not pool readings across hardware
or precision settings into one run.

The five objectives are output tokens/s (maximize), TTFT p50/p90 and TPOT
p50/p90 (minimize), with 3% relative noise tolerance. Request p90 latency is
also recorded as a diagnostic. Smoke mode is unscored. AgentX measures serving
performance; the numerical gate checks model execution, not coding-task success.

No stock CUDA image is declared as an AMD comparison baseline. Add one only
after it actually serves Flint correctly on the target hardware.

## Upstream references

- [Model card and BF16 weights](https://huggingface.co/RecursiveSI/flint)
- [Architecture and reference attention behavior](https://huggingface.co/docs/transformers/model_doc/flint)
- [vLLM recipe and its hardware-validation limits](https://recipes.vllm.ai/RecursiveSI/flint)

## INT4 on one MI210

Select [`mi2101x-int4`](.vibesys/tasks/mi2101x-int4/OBJECTIVE.md) to implement
and optimize the official [Flint INT4 GGUF](https://huggingface.co/RecursiveSI/flint-int4-gguf)
on one 64 GB AMD MI210. The fixed Q4_1 checkpoint is approximately 26 GB,
with additional cache and workspace needs. This task preserves the published
trained quantization; it does not optimize a new quantization recipe.

The [quantized checker](.vibesys/shared/int4_checker.py) builds a pinned,
CPU-only llama.cpp reference and scores against the same SHA256-verified GGUF.
It reuses the BF16 checker's response validation and numerical error budgets,
but does not load BF16 reference weights. Provision at least 40 GiB available
host RAM for the reference plus candidate host memory, a C++17 compiler,
CMake and OpenSSL development headers. Source builds use two jobs; allow
5 GiB temporary build space and 8 GiB build memory. Full-size gate runtime,
numerical tolerances and AMD execution remain unmeasured.

The model and source revisions and hashes are declared in the quantized
checker; task-scoped metadata provisions the quantized weights. AgentX uses
the original pinned base tokenizer, the quantized served-model identifier,
and the same four-tree/five-objective recipe. Keep its frontier separate from
the two BF16 targets. No candidate engine or verified AMD baseline is supplied.
