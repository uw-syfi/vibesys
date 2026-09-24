# vLLM profiling capture

How to fill a generic capture tool's lifecycle arguments (`command`, `env`,
`cwd`, `ready_command`, `load_command`, `stop_signal`, `grace_s`,
`timeout_s`) to get a clean profile against vLLM. See
[`tooling/profiling-serving-engines.md`](../tooling/profiling-serving-engines.md)
for what these arguments mean and the cross-engine problem they solve; this
file is vLLM's answers.

## Prerequisites

Read the index at
[`tooling/profiling-serving-engines.md`](../tooling/profiling-serving-engines.md)
first: it explains why a naive server-wrap capture can silently produce an
empty trace, and what problem each argument below is fixing.

## Multi-process by default

vLLM v1 forks a separate `EngineCore` subprocess from the API server
process; tensor-parallel serving adds one worker process per GPU on top of
that. Set `VLLM_ENABLE_V1_MULTIPROCESSING=0` to keep the engine in the same
process as whatever you launch, removing the fork that can fail to exit
cleanly under an external kill.

## Preferred: an offline single-process script

Build `vllm.LLM(...)` directly and call `.generate(...)` instead of
`vllm serve`. The whole process then exits on its own once generation
finishes, which is exactly the condition a clean-exit-only capture tool
needs. Verified end to end (rocprofv3 system trace, ROCm) on this exact
shape:

```
command = "python3 offline_generate.py --model <model> [--enforce-eager] \
  --num-prompts <n> --max-tokens <n> --warmup-prompts <n> --warmup-tokens <n>"
env = {"VLLM_ENABLE_V1_MULTIPROCESSING": "0"}
```

`offline_generate.py` does one untraced warmup `generate()` call first: in
HIP/CUDA-graph mode this is where graph capture happens, so the traced
window holds only steady-state replay kernels, not one-time capture
kernels. It then calls `generate()` again over the real batch inside the
traced region. No `ready_command`/`stop_signal` are needed for this shape:
the process is not a server, so there is nothing to poll or signal.

## If a server capture is required

Use this only when the objective concerns the HTTP path itself, not just
the engine. Shut the server down through its own graceful path rather than
a hard kill, and give it a generous grace period: the post-exit flush to
disk can itself take minutes on a large capture, scaling with how much was
traced, not with how long the run took.

```
command = "vllm serve <model> ..."
env = {"VLLM_ENABLE_V1_MULTIPROCESSING": "0"}
ready_command = "curl -sf http://127.0.0.1:$PORT/v1/models"
load_command = "<benchmark command> --url http://127.0.0.1:$PORT ..."
stop_signal = "SIGINT"
grace_s = 300
```

**Verified:** a graceful `SIGINT` to the server process group does make
rocprofv3 flush a complete, non-empty trace (confirmed: a full-size
`kernel_trace.csv` matching the offline-script path's output), in both
default multiprocessing and the single-process (`VLLM_ENABLE_V1_MULTIPROCESSING=0`)
mode -- the engine's own process topology (forked worker or not) does not
by itself block the flush the way it was assumed to. What *is* still
reliably true: the server process group rarely exits cleanly within a
typical grace period even after that flush -- something else in its own
shutdown/event-loop path keeps it alive -- so expect the capture's overall
status to read as an escalated/forced-kill outcome rather than a clean
exit, in both multiprocessing modes, even when the trace itself is
complete and safe to analyze. A capture tool that discards non-clean-exit
captures outright will discard a real trace this way; one that still
attempts analysis on a non-clean-exit capture (checking what's actually on
disk) surfaces the data correctly. Still prefer the offline single-process
path when the objective is engine-internal (no HTTP path involved): fewer
moving parts, and its capture completes with a clean exit rather than
needing escalation.

## torch.profiler interface: check the installed build

This has changed across vLLM releases and is not safe to assume:

- Some builds wire a `VLLM_TORCH_PROFILER_DIR` environment variable to
  `POST /start_profile` / `POST /stop_profile` HTTP endpoints on a running
  server.
- Other builds (confirmed on a recent vLLM build, ROCm 7.2.3 / torch 2.12)
  have no such env var wired at all: `POST /start_profile` 404s
  unconditionally, and instead accept a `profiler_config` keyword argument
  to `vllm.LLM(...)`, with `start_profile()`/`stop_profile()` methods called
  directly in an offline script.

Check which contract the installed build actually exposes before writing a
capture command against it:

```
python3 -c "from vllm.config.profiler import ProfilerConfig; import inspect; print(inspect.signature(ProfilerConfig))"
```

For an in-process `torch.profiler` capture against the `profiler_config`-
style build, the offline script looks like:

```python
llm = LLM(
    model=..., profiler_config={
        "profiler": "torch",
        "torch_profiler_dir": "<out_dir>",
        "torch_profiler_record_shapes": True,
    },
)
llm.generate(warmup_prompts, warmup_sampling)  # untraced warmup
llm.start_profile()
outputs = llm.generate(prompts, sampling)
llm.stop_profile()
```

This writes a gzipped Kineto trace (`*.pt.trace.json.gz`). Graph mode
produces far fewer CPU-side events than eager mode, since graph replay
collapses many per-kernel dispatch events into one graph-launch event.

## Post-capture hang (confirmed on ROCm)

Measured on both ROCm 6.4/torch 2.9.1 and ROCm 7.2.3/torch 2.12.
`stop_profile()` (or exiting the `torch.profiler` context) can hang for
several minutes with no further log output after the trace file is already
complete and valid on disk. This is a process-teardown hang, not a
data-loss issue: treat the trace file's size/mtime going stable as the
success signal, not the process's exit code. Wrap the capture in an outer
`timeout_s`, generously sized, and treat a timeout-forced exit as
success-with-a-known-hang once the trace file is confirmed on disk.

## Process topology

Profile the worker process that actually issues kernels, not the
API-server/driver process: a driver-only capture reads as idle by
construction, which is "wrong process," not "no bottleneck." For
tensor-parallel serving, capture each worker process independently.

## See also

- [`vllm.md`](vllm.md): vLLM source-code lookup (not profiling-specific)
- [`tooling/profiling-serving-engines.md`](../tooling/profiling-serving-engines.md): the generic capture-argument contract this file fills in
- [`platforms/`](../platforms/): the selected backend's capture mechanics (rocprofv3, nsys, ...)
