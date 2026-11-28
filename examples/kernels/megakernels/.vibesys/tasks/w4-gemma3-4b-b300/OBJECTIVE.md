# Gemma 3 4B with 4-bit weights decode step as one megakernel on NVIDIA B300

Write `b300/w4-gemma3-4b/submission.py`, and any helper files of your own under `b300/w4-gemma3-4b/`, so that
`build(case)` returns `run(inputs)`, which computes the whole google/gemma-3-4b-it decode step that
`.vibesys/shared/megabench/tasks/gemma.py` defines, in **one GPU kernel launch per step**:
one kernel that runs the whole step as phases. `build` receives the geometry in
`.vibesys/tasks/w4-gemma3-4b-b300/case.json` and may compile and allocate once; `run` takes the inputs
that `make_inputs` in the same module builds and returns `logits`, `next_token`, `k_write`, `v_write`. The score is the step's
kernel latency (device-event p50, `kernel_latency_ms`), lower is better.

The model is [google/gemma-3-4b-it](https://huggingface.co/google/gemma-3-4b-it) at batch 1. The reference module and the
helpers it imports are the exact PyTorch math, the input layouts and the tolerances.

## Rules

- Every output must match the PyTorch reference within the case's tolerances, on fresh random
  inputs and weights each time; integer outputs such as tokens must match exactly.
- Compute every output from the current inputs and weights on every call. Never modify an
  input.
- At most one GPU kernel launch per step, counted by the profiler; the check fails a step over
  the budget.
- Edit only `b300/w4-gemma3-4b/`. Never edit `.vibesys/`.

## Machine

One NVIDIA B300 GPU, the CUDA 13 toolkit with `nvcc` and Nsight Compute (`ncu`),
and a CUDA build of PyTorch in the default Python. `torch.utils.cpp_extension` can
JIT-compile `.cu` sources; compile for the device's own compute capability.

## Check and benchmark

Both commands run `.vibesys/shared/evaluate.py` on `b300/w4-gemma3-4b/submission.py`. The check builds
the submission, compares three trials against the reference and audits the launches of one
step. The benchmark repeats the check and times the step; a candidate that fails any part has
no `kernel_latency_ms` reading.
