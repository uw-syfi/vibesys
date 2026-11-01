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
the task when creating a run. This input provides no serving engine or
pre-cloned engine source: the candidate implements the sparse-MoE serving
stack for `Qwen/Qwen3.6-35B-A3B` from scratch on the selected hardware.
The H100 `default` task supports Modal-backed runs.

## AMD AgentX tasks

This project also offers two tasks over the same candidate codebase:

- [`mi300x-agentx`](.vibesys/tasks/mi300x-agentx/OBJECTIVE.md): one AMD MI300X,
  192 GB HBM. Start here for a single-device BF16 serving path with room for
  long-context cache and scheduling experiments.
- [`mi2104x-agentx`](.vibesys/tasks/mi2104x-agentx/OBJECTIVE.md): four AMD MI210s,
  64 GB HBM each. Partition weights and optimize communication, expert dispatch
  and hybrid recurrent state across devices.

Select the task when creating a run. `default` targets two H100s and Request Factory.
The AMD tasks use [the shared AgentX evaluator](../../../resources/evaluators/agentx/README.md)
and five throughput/TTFT/TPOT objectives with 3% relative noise tolerance.
They pin weights and tokenizer to revision
`995ad96eacd98c81ed38be0c5b274b04031597b0`.

BF16 weights alone occupy roughly 70 GB. One 64 GB MI210 cannot hold them;
four MI210s provide 256 GB aggregate HBM, distributed across separate devices.
One MI300X provides 192 GB on one device. These are capacity estimates, not
measured peak-memory or throughput results. Cache, recurrent states, workspace
and runtime overhead are additional; native context capacity does not promise
four simultaneous maximum-length sessions fit without cache management.

For Linux x86-64 with Python 3.12 and ROCm 6.4, install AMD PyTorch explicitly
rather than using the legacy task's unqualified `torch` requirement:

```bash
uv venv --python 3.12
uv pip install 'torch==2.9.1+rocm6.4' --index-url https://download.pytorch.org/whl/rocm6.4
uv pip install -r requirements-agentx.txt
```

Neither AMD task supplies an engine or a validated comparison baseline.
Transformers provides reference utilities; its presence does not establish
that an optimized DeltaNet/MoE kernel path supports either GPU. The shared
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
