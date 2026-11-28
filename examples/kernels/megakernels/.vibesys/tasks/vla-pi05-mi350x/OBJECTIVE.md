# pi0.5 action chunk step as one megakernel on AMD MI350X

Write `mi350x/vla-pi05/submission.py`, and any helper files of your own under `mi350x/vla-pi05/`, so that
`build(case)` returns `run(inputs)`, which computes the whole lerobot/pi05_base action chunk step that
`.vibesys/shared/megabench/tasks/pi05.py` defines, in **one GPU kernel launch per step**:
one kernel that runs the whole step as phases. `build` receives the geometry in
`.vibesys/tasks/vla-pi05-mi350x/case.json` and may compile and allocate once; `run` takes the inputs
that `make_inputs` in the same module builds and returns `actions`. The score is the step's
kernel latency (device-event p50, `kernel_latency_ms`), lower is better.

The model is [lerobot/pi05_base](https://huggingface.co/lerobot/pi05_base) at batch 1. The reference module and the
helpers it imports are the exact PyTorch math, the input layouts and the tolerances.

## Rules

- Every output must match the PyTorch reference within the case's tolerances, on fresh random
  inputs and weights each time; integer outputs such as tokens must match exactly.
- Compute every output from the current inputs and weights on every call. Never modify an
  input.
- At most one GPU kernel launch per step, counted by the profiler; the check fails a step over
  the budget.
- Edit only `mi350x/vla-pi05/`. Never edit `.vibesys/`.

## Machine

One AMD Instinct MI350X GPU (gfx950, CDNA4: 256 CUs in 8 XCDs, 64-wide wavefronts,
160 KiB LDS per CU, 256 MB Infinity Cache, 8 TB/s HBM3E, 288 GB), ROCm 7.1 with
`hipcc`, `hiprtc`, Triton and `rocprofv3`, and the ROCm build of PyTorch in the default
Python: `torch.cuda` and `cuda:0` address the HIP device, and
`torch.utils.cpp_extension` compiles `.cu` sources with hipcc. Compile kernels with
`--offload-arch=gfx950`. There is no CUDA toolkit, `nvcc` or Nsight.

## Check and benchmark

Both commands run `.vibesys/shared/evaluate.py` on `mi350x/vla-pi05/submission.py`. The check builds
the submission, compares three trials against the reference and audits the launches of one
step. The benchmark repeats the check and times the step; a candidate that fails any part has
no `kernel_latency_ms` reading.
