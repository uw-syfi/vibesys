---
name: serving-studio
description: >-
  Predict LLM serving performance with the ServingStudio simulator before
  spending hardware time: reproduce the measured head, attribute simulated
  time to kernels and phases, run what-ifs, and freeze a prediction with its
  provenance in a plan so the measurement can confirm or refute it. Use when
  planning or profiling an LLM serving optimization and a ServingStudio
  endpoint or checkout is set up, or when asked to set one up.
---

# serving-studio

ServingStudio Sim composes measured kernel times into a model of a serving engine's scheduler
(batching, chunked prefill, KV admission, speculative decoding) and predicts throughput, TTFT,
TPOT and where time goes, in seconds of CPU and with no accelerator. Use it to choose what to
build and to say in advance what the build should show. The hardware still decides: a simulated
number is never the task's metric, never a measurement, and never a reason to promote.

A planner uses this skill to back a plan with a frozen prediction, a profiler to answer a question
with simulated evidence, and a lead to rank plans by predicted gain, to record how far each
prediction missed, and to explore designs for a handoff ([handoff.md](references/handoff.md)):
the design and the ordered items workers build. An implement worker does not need it; one that
extends the simulator for a handoff item does.

## Setup: once per host, never per agent

You use what is installed; you install nothing (a model-extension item of a handoff builds a
clone of its own: [handoff.md](references/handoff.md)). Two variables say what is:

- `SERVINGSTUDIO_API`: a ServingStudio public API endpoint. Unset means the hosted service, which
  covers only its published GPU presets.
- `SERVINGSTUDIO_SIM`: the absolute path of a built ServingStudioSim checkout, for what the
  hosted service does not offer (Trainium, a model it does not publish, what-if knobs).

Check before the first use, and put the line it prints in your evidence:

```bash
python3 <skill-dir>/scripts/servingstudio.py health
git -C "$SERVINGSTUDIO_SIM" rev-parse HEAD   # only with a local checkout
```

If neither answers, or step 1 finds no deployment to use, stop the simulator work, not the
planning: say so in one line, plan from measurements as you would without this skill, and write
no Prediction block. Installing or upgrading the copy agents use is host setup that needs the
user's approval and is never done in the middle of an optimization: [setup.md](references/setup.md).
Adding a model or hardware is simulator development: only as a handoff item, in a copy of its
own ([handoff.md](references/handoff.md)).

## Work

1. **Find the deployment.** List the deployments the task's budget allows (layouts, replica
   counts, GPU counts) before choosing any. For each, match a hosted preset member
   ([hosted-api.md](references/hosted-api.md)) or a local preset
   ([local-sim.md](references/local-sim.md)) in a table: model and checkpoint, precision,
   hardware, parallel layout, engine features (MoE dispatch, speculative decoding, prefix caching,
   chunked prefill, graph mode), scheduler limits and `max_model_len`; write `unknown` where the
   catalog does not say. Then decide, from what changes the work the simulator models:
   - **Match:** the same architecture, precision and hardware, and every engine feature that
     changes the work on this workload is modeled the way the engine runs it (speculative
     decoding, prefix-cache reuse, chunked prefill when prompts exceed a chunk, MoE dispatch).
     Weights do not change modeled cost: a base and an instruct checkpoint of one architecture
     match. Every step below applies.
   - **Proxy:** the same architecture and precision, but other hardware of the same vendor (an
     H200 preset for an H100 task), a scheduler that differs from the engine's (a barebone
     worker for vLLM), or a feature the catalog leaves `unknown`. Steps 2 to 4 run, their
     numbers rank changes against each other only, and step 5 may freeze a relative change,
     never an absolute value. Name each difference in the plan.
   - **None:** another architecture, another precision, or hardware of another vendor or kind.
     Stop the simulator work as Setup says. Porting a model or hardware costs far more than one
     plan: only a lead's handoff funds it, as a model-extension item.

   A feature is modeled only when the preset shows it: a parameter or axis in its catalog entry,
   or a report that breaks out its cost. One the entry does not name is `unknown`, not modeled;
   a capture tag on another model's traces says nothing about this preset. Then ask the same of
   the idea itself: when its mechanism is one the simulator does not model on any deployment
   (host scheduling, launch overhead, a feature the preset lacks), the simulator cannot price
   it. Skip steps 2 to 5 for that idea, and write why in the plan, with no Prediction block; a
   share the simulator does report may still bound it (step 4).
2. **Reproduce the head.** Simulate the configuration and the workload the benchmark measures
   (arrivals, prompt and output lengths, concurrency, acceptance rates as measured, not assumed),
   and compare with the measured value of the same revision: the mean of its measured samples,
   or the statistic the task's objective uses. The residual is (simulated - measured) /
   measured, signed. On a proxy it measures the proxy's difference too, so it says how far an
   absolute number is off, not whether the ranking holds. On a match above 10% in magnitude, the
   simulator does not describe this engine: use it to rank ideas only, as for a proxy, and say so
   in every number you hand on.
   Name the phase or kernel that disagrees only with measured timing that shows it; otherwise
   call the residual unattributed.
3. **Attribute.** Split simulated time by phase (prefill, decode, queueing), by kernel kind and by
   overhead (launch, padding, idle groups, communication), from the reports the simulator
   produced. A cost it does not model (host scheduling, tokenizer, network, client) is
   `unmodeled`, never zero. The largest share is where a change has leverage; a share below a
   few percent cannot carry a large claim.
4. **Ask what-ifs.** Two kinds, never mixed up:
   - A **bound**: scale a kernel kind or a phase by a factor no implementation is known to reach
     ("MoE prefill 2x faster gives at most +35%"). It caps a gain; it predicts nothing.
   - A **prediction**: a configuration the engine can run or a change whose cost is known (a
     measured microbenchmark, a kernel row that exists). Only this may be frozen.

   When the question is which deployment to run, simulate each budget-compatible deployment
   from step 1 that has a match or a proxy (not one of them), name the ones the catalog does
   not offer, and call the winner the best of those evaluated.
5. **Freeze the prediction** before anyone implements the idea, in the plan or hypothesis you
   hand on ([prediction.md](references/prediction.md)): the metric, the predicted value and the
   head's simulated value, the residual of step 2, and the provenance (endpoint or checkout,
   simulator revision, preset and parameters, workload, the command, the result directory).
   Never edit a frozen prediction afterwards; a new one is added beside it.
6. **Close the loop.** Once the candidate is measured, whoever records its outcome writes
   predicted against measured value and the relative miss beside the prediction. A miss that
   points at the model (a phase it does not model, a kernel row from another shape) is a finding
   of its own: say which.

## Rules

- Quote only numbers the simulator printed, with the result directory or simulation id they came
  from. Arithmetic on them is labeled as yours.
- Keep the simulated workload the benchmark's. A what-if on another workload is a different claim.
- The hosted service runs two simulations at a time and takes six submissions a minute per client:
  run sweeps on a local checkout, and keep hosted use to a few targeted runs per plan.
- Simulator output is evidence, not a file of the candidate: keep it where your brief puts
  scratch output, and never commit it with a candidate.
