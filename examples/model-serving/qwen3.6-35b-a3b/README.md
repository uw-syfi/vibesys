# Qwen3.6-35B-A3B serving

One candidate project supports these hardware and workload tasks:

| Task | Hardware | Workload |
| --- | --- | --- |
| `default` | 2 NVIDIA H100 GPUs | Fixed-text Request Factory |
| `mi2104x-agentx` | 4 AMD MI210 GPUs | AgentX |
| `mi300x-agentx` | 1 AMD MI300X GPU | AgentX |
| `mi2104x-tracelab` | 4 AMD MI210 GPUs | TraceLab recorded sessions |
| `mi300x-tracelab` | 1 AMD MI300X GPU | TraceLab recorded sessions |

Use `examples/model-serving/qwen3.6-35b-a3b` as the input project and select
the task when creating a run. The H100 `default` task supports Modal-backed
runs.

## Baseline engine

Every task starts from the baseline engine in [`engine/`](engine/), an
OpenAI-compatible server that runs and is slow:

- Transformers runs the BF16 checkpoint, pinned in `engine/checkpoint.json`,
  sharded over every visible GPU with `device_map="auto"`. The same code runs on
  two H100s, four MI210s or one MI300X. It refuses to start
  rather than offload weights to the host.
- One worker thread prefills new requests one at a time, in arrival order, then
  runs one decode step for every running request together: their caches are
  joined into one batch, shorter attention caches left-padded and masked, and
  split again after the step. Greedy outputs match those of the same request
  served alone; the CPU check holds this. There is no fused or custom kernel.
- A prefix cache keeps snapshots of the model cache: at prefill-chunk
  boundaries, and after all but the last token of each prompt. Usage reports
  the reused tokens as `prompt_tokens_details.cached_tokens`, which TraceLab's
  prefix-cache preflight requires.
- It serves `GET /health`, `GET /v1/models`, and streaming or non-streaming
  `POST /v1/completions` and `POST /v1/chat/completions`. It honors exact
  `max_tokens` with `ignore_eos`, `min_tokens`, stop strings, and greedy decoding at
  temperature 0. Chat uses the checkpoint's template with thinking off unless
  `chat_template_kwargs` enables it; reasoning streams as `reasoning_content`.
  It does not parse tool calls out of generated text.

It is not a validated performance baseline: nothing here has been measured on
any GPU. The service command, run from the project root, is:

```bash
python -m engine.server --host 127.0.0.1 --port <port>
```

Its readiness URL is `/health`; the port opens once the weights are loaded.
On Modal, [`main.py`](main.py) deploys the same command on two H100s with a
persistent Hugging Face cache volume.
[`.vibesys/shared/cpu_check/`](.vibesys/shared/cpu_check/) checks the HTTP contract
on CPU against tiny random checkpoints of the same architecture:

```bash
# Keep the CPU environment out of the project tree, which every evaluation copies.
UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/vibesys/qwen36-cpu-check-venv" \
  uv run --project .vibesys/shared/cpu_check python -m pytest .vibesys/shared/cpu_check
```

## AMD AgentX tasks

This project also offers two tasks over the same candidate codebase:

- [`mi300x-agentx`](.vibesys/tasks/mi300x-agentx/OBJECTIVE.md): one AMD MI300X,
  192 GB HBM. Start here for a single-device BF16 serving path with room for
  long-context cache and scheduling experiments.
- [`mi2104x-agentx`](.vibesys/tasks/mi2104x-agentx/OBJECTIVE.md): four AMD MI210s,
  64 GB HBM each. Partition weights and optimize communication, expert dispatch
  and hybrid recurrent state across devices.

Select the task when creating a run. `default` targets two H100s and Request Factory.
The AgentX tasks have five throughput/TTFT/TPOT objectives with 3% relative
noise tolerance. They pin weights and tokenizer to revision
`995ad96eacd98c81ed38be0c5b274b04031597b0`.

They are two-image tasks (`version = "0.2"`) that only vibesys-cli runs; this
app refuses them. The candidate's server runs in the image
[`Dockerfile.rocm`](Dockerfile.rocm) builds from the candidate's commit (context:
the project root, which the root `.dockerignore` filters to
`requirements-agentx.txt`), as `python -m engine.server --host 127.0.0.1 --port
<port>` from the checkout's root, with readiness `/health` and 30 minutes to
load. Each task's own `Dockerfile` is the task image: the
`vibesys-evaluator-llm-serving` image (release 0.2.0rc1, pinned by digest, with
`agentx-harness==1.0.7`) plus the task directory, where `checker.py` is a copy
of the shared service checker and `bench.py` runs the replay against
`VIBESYS_SERVICE_URL`. Both images read the operator's Hugging Face cache,
mounted read-only as `hf` (`HF_HOME=/vibesys/mounts/hf`, `HF_HUB_OFFLINE=1`);
it must already hold `Qwen/Qwen3.6-35B-A3B` at the pinned revision and the
prepared AgentX corpus `semianalysisai/cc-traces-weka-062126` under
`datasets/` (the replay copies it to a writable cache when the mount is
read-only).

`python /task/bench.py benchmark` is the scored full replay, the recipe of
[the earlier AgentX evaluator](../../../resources/evaluators/agentx/README.md).
`python /task/bench.py profile` is its smoke replay: two corpus entries for 60
seconds, never scored. Run it as a command in the task image against the
candidate's server to try the chat path or to record a profile.

BF16 weights alone occupy roughly 70 GB. One 64 GB MI210 cannot hold them;
four MI210s provide 256 GB aggregate HBM, distributed across separate devices.
One MI300X provides 192 GB on one device. These are capacity estimates, not
measured peak-memory or throughput results. Cache, recurrent states, workspace
and runtime overhead are additional; native context capacity does not promise
four simultaneous maximum-length sessions fit without cache management.

For Linux x86-64 with Python 3.12 and ROCm 6.4, install AMD PyTorch explicitly,
then the engine's other pins. The H100 task's `requirements.txt` has the same
pins with the CUDA build of torch.

```bash
uv venv --python 3.12
uv pip install 'torch==2.9.1+rocm6.4' --index-url https://download.pytorch.org/whl/rocm6.4
uv pip install -r requirements-agentx.txt
```

This torch build cannot run `torch._grouped_mm`, Transformers' default MoE
experts kernel: it raises "grouped gemm is not supported on ROCM" at the first
forward pass, even though Transformers' availability check passes (an MI250
reports compute capability 9.0). Transformers' `eager` experts run, but
synchronize with the host once per selected expert, so decode is slow. The
baseline engine therefore registers its own experts implementation,
`baseline_matmul` in `engine/experts.py`, and loads it whenever
`torch.version.hip` is set (`--experts-implementation` overrides it). It uses
only ordinary matmuls: Transformers' `batched_mm` for decode-sized inputs, and
for prefill chunks a loop over the experts that received tokens.

Neither AMD task supplies a validated comparison baseline. The baseline engine
uses Transformers' reference DeltaNet and MoE code; that it runs does not
establish that an optimized kernel path supports either GPU. The shared
H100 service checker is behavioral, not a numerical parity check. Its short
chat probes need a non-thinking default; explicit chat-template settings and
reasoning/tool history from the benchmark must still be preserved. Full-size
correctness and AgentX execution have not been measured on either AMD target.

## TraceLab recorded-session tasks

Select `mi2104x-tracelab` or `mi300x-tracelab` for the same model/hardware
contracts with a different workload. These replay all 48 sessions / 303 rounds
of the pinned recorded TraceLab sample through Request Factory, with eight
active sessions, prefix reuse and tool waits. They use the same five objectives
and 3% noise tolerance. See [the adapter's provenance and measurement policy](.vibesys/shared/tracelab/README.md).

The sample is recorded, not statistically resampled, but it is much smaller
than the full TraceLab corpus. Keep its frontier separate from AgentX and the
legacy H100 fixed-text task. Its behavioral accuracy gate has the same limits
as the AMD AgentX tasks.
