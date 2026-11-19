# Acceptance ladders

In simulator-led mode ([simulator-led.md](simulator-led.md)), each plan is accepted on the
cheapest evidence that settles it. Each kind has a ladder. The worker climbs it in order and stops
at the first rung that fails. You accept the plan once its last rung passes. A rung that fails
sends the plan back to its worker with the evidence, or parks it when the failure is about the
idea and not the code. None of these rungs is the task's full check or a measure. Those come only
with the integration ([integration.md](integration.md)).

"The bar" is the task's declared `min_gain`, or, when the task declares none, the noise bar of
[integration.md](integration.md#when-to-integrate). A prediction always comes from the simulator
under the newest calibration record, in the domain skill's frozen-prediction format.

## kernel

1. **CPU tests.** The kernel's correctness tests against a reference, at the shapes the engine
   runs, in the worker's worktree (`kernel-correctness`). The candidate commit says
   `Checks: pass`.
2. **On-device self-test.** The same comparison on the device, as a device job:
   `vibesys-cli submit command <sha> --image harness --gpus <n> --plan <p> --note "<question and
   prediction>" -- <self-test argv>`. On Trainium, `--gpus N` takes N contiguous NeuronCores. The
   self-test catches in minutes a failure that a full serving run would find hours in.
3. **Microbenchmark.** The kernel's timings at the engine's shapes, as a device job that keeps its
   results: add `--outputs`, write the timings under `$VIBESYS_ARTIFACTS_DIR`, and copy them back
   with `vibesys-cli outputs <job> --to <scratch dir>`. `outputs` prints each file's size and
   SHA-256. Name the job id and the file's SHA-256 wherever the timings are used.
4. **Re-predicted end-to-end gain.** A profiler imports the timings into the simulator through
   the domain skill's calibration procedure. That makes a new calibration record, which names the
   microbenchmark job. The profiler then simulates the head and the candidate under it. Accept the
   plan when that predicted gain exceeds the bar. Otherwise park it, with the prediction.

## system

1. **Simulator with current timings.** Before you fund it: the plan's prediction under the newest
   calibration, with every microbenchmark imported so far, exceeds the bar.
2. CPU tests in the worktree, as for a kernel.
3. **Short on-device smoke check.** A device job that runs the engine on a few requests and checks
   its outputs. It takes minutes, not the task's full check.

## layout

1. **It must win in the simulator on measured timings first.** Fund a layout plan only once its
   prediction exceeds the bar under a calibration whose kernel times were measured. A scenario in
   which a kernel is assumed faster does not count. Until then the plan waits on the roadmap, and
   you predict it again whenever new timings arrive.
2. Then the system ladder from its rung 2: CPU tests and a short smoke check.

## simulator

Implement the brief on a checkout of the simulator, not of the task's repository. Use a worktree
of the domain skill's simulator checkout, in the worktrees directory. Accept the plan when the
simulator's own tests pass and the change does what the plan claims. The plan's claim is either
that the simulator reproduces the measured head within the calibration's residual, or that it
models what a waiting plan needs. A simulator plan runs no device job and makes no candidate of
the task.

## probe

A probe states one question and what each answer would change. It ships nothing. Its device jobs
answer the question. Accept it when the answer is written on the plan:
`answer: <the answer>; evidence: <job ids>; changes: <the plans or roadmap items it moves>`.

## Closing a plan that ships nothing

`plan list --ready` lists a plan only when every plan it depends on is `won`. So close an accepted
simulator plan or answered probe plan `won`, with its acceptance comment. Close a probe that
cannot be answered `rejected`, and say why. Kernel, system and layout plans stay `chosen` once
accepted, with the `accepted:` comment of [integration.md](integration.md), and they reach `won`
through an integration. Write the comment as
`accepted: <SHA> built on <parent>; evidence: <each rung, with its job id or the file's SHA-256>;
predicts <share> of the headline (under calibration <id>)`.

## Device jobs

A device job is any job that runs on the task's accelerator: a check, a measure or a command. Each
one states the question it answers and the prediction it tests, in its note:

```
--note "Question: <what this job settles>. Predicts: <the outcome the plan expects, in numbers>."
```

The worker lists every device job it may submit in its brief (`Device jobs:`). After each handoff
and each finished job, look at the plan's jobs (`vibesys-cli plan view <n>`, or `vibesys-cli jobs
--json` for their notes). Refuse a device job whose note is missing either part. If it is queued
or running, cancel it: `vibesys-cli cancel <job> --reason "states no question or prediction"`.
Its result is not evidence for any rung. Send the brief back. An integration's check and measure
carry the integration plan's question and its composed prediction.
