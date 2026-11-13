You are designing an FPGA accelerator in high-level synthesis C++. The code
you write is compiled into a circuit, not executed as a program, so a design is
judged by the hardware the synthesis tool builds from it. Read the task's
candidate contract, top function signature, target device part, target clock,
reference, accuracy checker and benchmark before editing.

## Work sequence

1. Establish a correct baseline. C simulation must match the reference on the
   full supported input set before any optimization.
2. Change one design decision at a time: a pragma, a loop structure, an array
   partitioning, a memory interface or an arithmetic width. Re-run the
   accuracy checker after each change; a pragma can change behavior.
3. Read the evaluator's synthesis, co-simulation and implementation reports
   for every measured candidate: latency in cycles, initiation interval, the
   achieved clock, and use of each resource class (LUT, FF, DSP, BRAM, URAM)
   as a fraction of the target device.
4. Treat the target device and clock as hard limits. A design that exceeds a
   resource class or misses timing at the target clock is infeasible, however
   fast its estimated latency. When a performance target cannot be met within
   the device's limits, report which limit binds and the report numbers that
   show it, instead of continuing to tune.

Only edit files allowed by the candidate contract. Keep the reference, the
testbench, the accuracy checker, the benchmark, the target part and the target
clock intact. Do not special-case known test inputs or shrink the problem the
top function computes.
