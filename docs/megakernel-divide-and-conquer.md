# Writing Megakernels

A megakernel runs a whole model step, such as one decode step of an LLM, as
phases inside a single GPU kernel launch. The `optimize` skill of the
`vibesys` plugin has a mode for this kind of task, selected with
`--megakernel-divide-and-conquer`. This page says when to use it, what the
task needs, and what a run leaves behind. The skill's own instructions are in
[`megakernel-divide-and-conquer.md`](https://github.com/uw-syfi/vibesys/blob/main/skills/search/optimize/references/megakernel-divide-and-conquer.md).

## How the mode works

Phases that share one launch also share its registers, shared memory and
barriers, so a phase that gets faster on its own can slow the others down. The
mode therefore divides the kernel at its phase boundaries but judges every
change by its effect on the whole step:

1. **Bootstrap.** A worker writes a correct single-launch kernel.
2. **Map.** A profiler times each phase inside the launch and writes a *phase
   map*: each phase's share of the step, its speed-of-light floor and the
   resources it may use.
3. **Conquer.** The phases with the largest gaps go to workers in parallel,
   one phase each, under a contract that fixes the phase's inputs, outputs and
   resource budget. One more worker can own a change across phases, such as
   cheaper synchronization between them.
4. **Glue.** A combiner merges the accepted phases into one kernel, which is
   measured as a whole. It moves the result branch forward only after a judge
   passes it and it measures faster than the current best.
5. **Re-map** the new kernel and repeat until the budget is spent.

## When to use it

Use the mode only when the candidate is one kernel launch that runs the whole
step as phases. For other kernel tasks, including ones with a few fused
kernels, use the default loop.

The mode needs a way to time phases inside the kernel. On CUDA this is the
`kernel-timeline-analysis` skill of the `vibesys-kernels-cuda` plugin. On
other backends, or when the task provides its own timeline tool, the lead's
first plan builds a phase timer before dividing the kernel.

## Prepare the task

1. Install the `vibesys` plugin and the kernels plugin for your hardware
   (`vibesys-kernels-cuda`, `vibesys-kernels-rocm` or
   `vibesys-kernels-trainium`), as in the
   [skills installation guide](https://github.com/uw-syfi/vibesys/blob/main/skills/README.md#install).
2. Create the task with `/vibesys:create-task`. Its check should compare
   every output with a reference implementation on fresh inputs, and count
   kernel launches so a candidate with more than one launch per step fails.
   Its benchmark should measure the latency of one full step.

The workloads of the megakernel blog post are tasks of this kind, ready to copy:
`vibesys example new kernels/megakernels ~/work/megakernels` gives a dense and an
MoE LLM, two quantized LLMs, a text-to-speech model, a vision-language-action
model and a world model, each with its check and benchmark
([`examples/kernels/megakernels`](https://github.com/uw-syfi/vibesys/tree/main/examples/kernels/megakernels)).

## Start the run

```text
/vibesys:optimize <task> --megakernel-divide-and-conquer --budget time 285m --fanout 3
```

- `--megakernel-divide-and-conquer` selects the mode. An operator steering
  note that asks for it does the same.
- `--budget time <duration>` or `--budget cycles <n>` bounds the run. Without
  a budget, the run continues until you stop it. A cycle that finds nothing
  does not end the run; the lead escalates to boundary changes, a redesign, a
  finer noise bar or a finer phase map instead.
- `--fanout N` caps the subagents running at once (default 3). Planners,
  workers, profilers, combiners and judges all count.

Give the agent a budget a little below any hard limit of the harness or
cluster, so the last integration, its measurement and the final report fit.

## What a run leaves

- The best kernel on the result branch `vibesys/result/<task>`. Every
  promotion on it passed the check, a judge and a comparison against the
  previous best.
- The numbered phase maps, one per head, in a `<repository>.phases/<task>/`
  directory beside the worktrees. They show where the time went after each
  cycle.
- A final report with the cycles run, the budget spent and the remaining gap
  of each phase to its floor.
