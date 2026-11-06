# Engine profiling conventions

Four things a serving engine does so that a profiler wrapped around its server command produces a
trace you can read. A bespoke engine you write should do all four from the start; an upstream
engine (vLLM, SGLang) does some of them, and its file under [`engines/`](../engines/) says which.
The capture mechanics themselves (which profiler, which flags, how a job wraps the server) are the
platform's: on ROCm, the `kernel-rocprof-analysis` skill (plugin `vibesys-kernels-rocm`).

## 1. Name the phases with ranges

Wrap each phase of a step in a profiler range: scheduling (batch formation, KV-block allocation),
prefill, decode, sampling, and any KV transfer or communication. A kernel trace alone says which
kernels ran; ranges say which phase each belongs to, so "decode is 70% of GPU time" becomes
measurable instead of inferred from kernel names.

- Use the platform's range API: roctx on ROCm, NVTX on CUDA. From PyTorch,
  `torch.cuda.nvtx.range_push(name)` / `range_pop()` emit roctx ranges on ROCm builds and NVTX
  ranges on CUDA builds, so one call site serves both.
- Use a small, fixed set of names (`prefill`, `decode`, `schedule`, ...), not per-request names: a
  range name is a grouping key, and unbounded names make the trace unreadable.
- Ranges cost a call per push and pop. Put them around steps, never inside a kernel loop, or gate
  them on an environment variable your engine reads.
- The profiler records ranges only when asked (rocprofv3: the marker trace).

## 2. Warm every compiled variant before reporting ready

The readiness endpoint must answer 2xx only after the engine has compiled, tuned and captured every
variant the load can reach: each batch-size and sequence-length bucket, every graph to capture,
every JIT kernel (Triton, AITER, torch.compile) and every GEMM tuning lookup. A variant first met
under load compiles inside the measured window: the benchmark pays for it and the capture shows
compilation where the steady state should be. Warm by running the real step function over each
bucket, not by a single dummy request.

## 3. Open the profiling window when warm, close it on shutdown

When the profiler collects only between pause and resume calls (rocprofv3 `--selected-regions`,
which follows `roctxProfilerResume` / `roctxProfilerPause` from the roctx library of the image's
ROCm release), the engine decides the window:

- start paused (the profiler starts the process that way), resume right after warm-up and before
  reporting ready, and pause again when shutdown begins;
- do it only when asked, through an environment variable your engine defines and the job sets in
  its argv prefix (`env MY_ENGINE_PROFILE_WINDOW=1 python3 tools/capture.py timeline
  --selected-regions --`), because an unprofiled run has nothing to resume.

A window that follows the engine's own warm-up is exact without guessing how long startup takes,
which a fixed collection delay must.

## 4. Let the profiler see every process and exit cleanly

- **Children inherit the environment.** A profiler attaches to a process through environment
  variables it sets before starting it (preloaded libraries, tool paths). A worker process (tensor
  or pipeline parallel ranks, a detokenizer, a scheduler process) is traced only if it inherits
  that environment. Start workers with the parent's environment plus your additions, never a
  fresh or minimal one, and do not route them through a launcher that scrubs it. Each traced
  process writes its own output directory.
- **SIGTERM means exit normally.** The job stops the server with SIGTERM and waits before killing
  it. Profilers write their trace at normal process exit, so on SIGTERM: stop accepting requests,
  pause the profiling window, tell workers to stop, wait for them, and return from `main` (exit
  handlers must run). Never end with `os._exit`, and never SIGKILL a worker that has not finished
  writing; its trace is lost. Exit well within the job's grace period; a large trace takes time to
  write.

## Checking an engine against the conventions

Capture one timeline with the marker trace and the selected-regions window, and confirm in its
output that: every process you expect has an output directory; the ranges appear with the fixed
names; no compilation or tuning kernels appear inside the window; and the job's log shows a clean
exit for every process.
