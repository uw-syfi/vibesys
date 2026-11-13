---
name: fpga-hls-workflow
description: How an FPGA HLS candidate is built, checked and scored in a VibeSys run, and which edits the judge rejects. Use when starting an HLS task, reading the evaluator's C simulation, synthesis, co-simulation or implementation results, or deciding which stage to wait for.
---

# FPGA HLS workflow

The candidate is HLS C++ that the synthesis tool compiles into a circuit for
the task's target device part and clock. You edit only the files the task's
candidate contract allows. The task's evaluator runs the Vitis flows and owns
their configuration; you read their results.

## Stages, cheapest first

1. **C simulation** compiles the design as ordinary C++ and runs the
   testbench against the reference. Seconds. It proves function, not timing:
   it ignores pragmas and cannot see hardware deadlocks.
2. **C synthesis** produces RTL plus estimates: latency in cycles, achieved
   initiation interval per loop, estimated clock, and resource use. Minutes.
   Estimates can be optimistic about timing and routing.
3. **C/RTL co-simulation** runs the generated RTL cycle by cycle against the
   same testbench. It gives the measured cycle count and catches stream
   deadlocks and interface mismatches that C simulation misses.
4. **Implementation** places and routes the design on the device. It reports
   the real resource use and timing slack at the target clock.

Falsify a hypothesis at the cheapest stage that can: a pragma that should
lower II is visible in the synthesis report, not only after implementation.
When stages disagree, the later stage is the evidence.

## Reading results

- Latency in time is cycles times the clock period the design achieves, not
  the period it targets.
- A loop that misses its target II says why in the synthesis log: a
  loop-carried dependency, too few memory ports, or a long operation chain.
- Feasibility is a separate gate from speed; see the `fpga-hls-feasibility`
  skill.

## Edits the judge rejects

- Editing the testbench, reference, checker, benchmark, target part, target
  clock or tool configuration.
- Shrinking what the top function computes: smaller shapes, fewer outputs or
  narrower arithmetic than the contract permits.
- Pragmas or directives whose only effect is on the reported numbers, such as
  trip-count hints that disagree with the real loop bounds.
- Outputs derived from known test inputs instead of computed from runtime
  inputs.
