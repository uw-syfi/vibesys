# Qwen3.6-35B-A3B serving

One candidate project supports these hardware and workload tasks:

| Task | Hardware | Workload |
| --- | --- | --- |
| `mi2104x-agentx` | 4 AMD MI210 GPUs | AgentX |
| `mi300x-agentx` | 1 AMD MI300X GPU | AgentX |
| `mi2104x-tracelab` | 4 AMD MI210 GPUs | TraceLab recorded sessions |
| `mi300x-tracelab` | 1 AMD MI300X GPU | TraceLab recorded sessions |
| `mi2508x-tracelab-scale` | 1 AMD MI250 node (8 GCDs) | TraceLab concurrency sweep |

Every task is a two-image task (`version = "0.2"`) that only vibesys-cli runs,
with `examples/model-serving/qwen3.6-35b-a3b` as the project; name the task
when submitting a job.

## Baseline engine

Every task starts from the baseline engine in [`engine/`](engine/), an
OpenAI-compatible server that runs and is slow:

- Transformers runs the BF16 checkpoint, pinned in `engine/checkpoint.json`,
  sharded over every visible GPU with `device_map="auto"`. The same code runs on
  four MI210s, one MI300X or an MI250 node, and on CUDA GPUs. It refuses to start
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
persistent Hugging Face cache volume, for development on CUDA; no task runs
there.
[`.vibesys/shared/cpu_check/`](.vibesys/shared/cpu_check/) checks the HTTP contract
on CPU against tiny random checkpoints of the same architecture:

```bash
# Keep the CPU environment out of the project tree, which every evaluation copies.
UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/vibesys/qwen36-cpu-check-venv" \
  uv run --project .vibesys/shared/cpu_check python -m pytest .vibesys/shared/cpu_check
```

## AMD AgentX tasks

Two tasks replay AgentX over the same candidate codebase:

- [`mi300x-agentx`](.vibesys/tasks/mi300x-agentx/OBJECTIVE.md): one AMD MI300X,
  192 GB HBM. Start here for a single-device BF16 serving path with room for
  long-context cache and scheduling experiments.
- [`mi2104x-agentx`](.vibesys/tasks/mi2104x-agentx/OBJECTIVE.md): four AMD MI210s,
  64 GB HBM each. Partition weights and optimize communication, expert dispatch
  and hybrid recurrent state across devices.

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
of [the shared service checker](.vibesys/shared/accuracy_checker/) and `bench.py` runs the replay against
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
candidate's server to try the chat path or to record a profile:

```bash
vibesys-cli submit command <sha> --task mi2104x-agentx --image harness -- python /task/bench.py profile
```

BF16 weights alone occupy roughly 70 GB. One 64 GB MI210 cannot hold them;
four MI210s provide 256 GB aggregate HBM, distributed across separate devices.
One MI300X provides 192 GB on one device. These are capacity estimates, not
measured peak-memory or throughput results. Cache, recurrent states, workspace
and runtime overhead are additional; native context capacity does not promise
four simultaneous maximum-length sessions fit without cache management.

For Linux x86-64 with Python 3.12 and ROCm 6.4, install AMD PyTorch explicitly,
then the engine's other pins. `requirements.txt` has the same pins with the
CUDA build of torch.

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
service checker is behavioral, not a numerical parity check. Its short
chat probes need a non-thinking default; explicit chat-template settings and
reasoning/tool history from the benchmark must still be preserved. Full-size
correctness and AgentX execution have not been measured on either AMD target.

## TraceLab recorded-session tasks

Select `mi2104x-tracelab` or `mi300x-tracelab` for the same model/hardware
contracts with a different workload. These replay all 48 sessions / 303 rounds
of the pinned recorded TraceLab sample through Request Factory, with eight
active sessions, prefix reuse and tool waits. They use the same five objectives
and 3% noise tolerance. See [the adapter's provenance and measurement policy](.vibesys/tasks/mi2104x-tracelab/tracelab/README.md).

Like the AgentX tasks, they are two-image tasks that only vibesys-cli runs, with
the same candidate image. Each task's `Dockerfile` is the task image: Request
Factory's `session_runner`, built from the revision the earlier
`vibesys-evaluator-request-factory` evaluator pinned, the shared service checker
(`checker.py`) and the adapter (`tracelab/`), which reads the pinned tokenizer
from the same read-only Hugging Face mount. The two task directories differ
only in `OBJECTIVE.md` and the hardware in `vibesys.input.toml`.

The sample is recorded, not statistically resampled, but it is much smaller
than the full TraceLab corpus. Keep its frontier separate from AgentX. Its
behavioral accuracy gate has the same limits as the AMD AgentX tasks.
