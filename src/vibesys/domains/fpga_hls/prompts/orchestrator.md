Plan FPGA accelerator work around the task's reference, target device, target
clock and the evaluator's reports. Feedback is slow: C simulation takes
seconds, synthesis minutes, and co-simulation and implementation can take far
longer. Write each round as a testable hypothesis whose first evidence comes
from the cheapest stage that can falsify it.

Useful round boundaries include loop pipelining and initiation interval,
unrolling against resource use, array partitioning and memory banking, memory
interface width and burst access, dataflow between stages, and arithmetic
width. Track how close the best design is to each device limit: a direction
that needs more of a resource class than the device has, or a shorter clock
period than the design closes timing at, is infeasible on this target and
should be retired with the binding limit and its report numbers recorded.
