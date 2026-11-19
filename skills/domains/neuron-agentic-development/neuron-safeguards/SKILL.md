---
name: neuron-safeguards
description: "Required safeguards for any change that runs on AWS Trainium (Neuron): a checklist a lead copies into every implement and combine brief and a planner checks before freezing a plan. Use when briefing, planning or implementing a change for a task on the trainium backend, above all one that adds an NKI kernel, compiles with neuronx-cc, or must pass an on-device correctness check."
---

# Neuron safeguards

Implementers on a Trainium task have no device of their own: they test on CPU and the NKI CPU
simulator, and the device runs only inside measure and command jobs. Several failures show only
there, late, and cost a whole protected check. These safeguards make them fail fast and cheap.

## Checklist

Lead: copy this block whole into every implement and combine brief. Planner: check your plan
against items 4 and 5 before you write its body.

```
Safeguards (Neuron):
1. Self-test every new kernel on the device at startup, before serving or benchmarking: run it
   on small fixed inputs at the largest shapes the run uses, compare it with the reference path
   it replaces within the task's tolerance, and on a mismatch or an error print both and exit
   non-zero. Keep the self-test on by default in the committed code.
2. Bound warm-up with a short stall timeout: when compile-and-load or the first request makes
   no progress for a few times the parent revision's warm-up, print where it stalled and exit
   non-zero. Never wait out the check's own timeout.
3. Run `neuronx-cc`, and anything that invokes it, with its working directory in a fresh
   directory under the temp dir (`cd "$(mktemp -d)"`, or `cwd=tempfile.mkdtemp()`): it writes
   its log into the working directory.
4. CPU tests, the NKI simulator and a CPU review do not show a change is correct on the device.
   Pass the task's correctness check through an on-device job before you claim. When it fails
   on some requests only, diagnose before blaming a race: identical prompts to every
   data-parallel group, many prompts per group, the candidate against the parent as control.
5. Every launch requirement the plan pins (flag, environment variable, compiler or runtime
   version, core count, memory) must also hold for the input revision: the baseline is checked
   with the same launch.
```

## Why each item

1. **Self-test.** The CPU simulator does not catch every out-of-bounds DMA. A kernel with one
   passed simulation and hung the protected check on the device for an hour. A startup self-test
   turns that hour into seconds and a non-zero exit names the kernel. Test the largest shapes,
   since out-of-bounds accesses show at the edges.
2. **Warm-up timeout.** A hung kernel or a deadlocked collective prints nothing and never exits.
   Measure the parent's warm-up from its job log and set the timeout from it, so a cold compile
   cache does not trip it but a hang does.
3. **Compiler working directory.** A compile started from the agent's workspace wrote its log
   where the agent may not write, and the turn failed. Set the working directory where the
   compiler is spawned, in both test scripts and the server's launch, not only in your shell.
4. **Marginal checks and on-device proof.** A candidate with about 900 lines of CPU tests and an
   independent CPU review failed long-context retrieval on about 10% of long prompts on the
   device. A check can also be marginal: in one passkey retrieval prompt the correct answer led by
   under one logit, so a numerically different but equally accurate kernel failed it
   deterministically, and the outcome depended on which data-parallel group served the request.
   Before blaming a race:
   - Send identical prompts to every data-parallel group. Groups that disagree on the same prompt
     every time differ in numerics, not timing.
   - Send many prompts per group, and repeat them. A race fails different prompts on each repeat;
     a margin fails the same ones.
   - Run the candidate and a control (the parent revision) on the same prompts, and report the
     correct answer's logit margin on each failing prompt.
   A failure on prompts whose margin is under one logit in the control too is a property of the
   check: say so with the margins, and do not change the kernel to chase it.
5. **Launch requirements.** A handoff pinned a launch requirement the run's input revision could
   not meet, so the input's baseline check failed at launch. Read the input revision
   (`git show <input sha>:<path>`) for each requirement the plan pins. When it cannot hold there,
   make the candidate's own code supply it instead of the launch.
