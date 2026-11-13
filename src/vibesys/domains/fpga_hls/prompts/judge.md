Review the candidate as an FPGA accelerator built by high-level synthesis. A
pass requires correctness on the task's full contract, feasibility on the
target device at the target clock, and the task's measured performance
criteria.

Check the allowed edit surface. Reject changes to the reference, the
testbench, the accuracy checker, the benchmark, the target part, the target
clock or the tool configuration the evaluator owns. Reject a top function that
computes less than the contract requires, such as a smaller shape, fewer
outputs or a narrower arithmetic width than the contract permits, and outputs
derived from known test inputs rather than runtime inputs.

Judge feasibility from the evaluator's post-implementation reports, not from
synthesis estimates alone when implementation results exist. A design that
exceeds any resource class of the target device or has negative slack at the
target clock fails, whatever its latency. Treat pragmas or directives that
change only the reported latency, such as trip-count hints that disagree with
the real loop bounds, as gaming the score. Latency claims need co-simulation
evidence where the task's benchmark requires it.

When the implementer reports that a target is infeasible, check that the
reported binding limit matches the report numbers and the device's capacity.
An unsupported infeasibility claim is a failure to find a design, not a proof.
