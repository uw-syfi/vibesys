Profile the invocations the task actually measures, with the same arguments,
inputs, environment, and build. Record the command, input size, repetition
count, and whether caches were warm, so the evidence can be compared with the
task's measurement.

Separate fixed cost from steady-state work. For a short-running program, check
dynamic linking, locale setup, configuration loading, argument parsing, and
allocator start-up before looking at the main loop. Compare a run on tiny input
with one on representative input to see what scales.

Then look at the costs that apply to the program, such as scanning, decoding,
pattern or expression compilation, parsing, directory traversal, metadata
calls, sorting, formatting, output volume, allocation, and subprocess creation.
Decide whether the workload is CPU-bound, syscall-bound, I/O-bound,
allocation-bound, or dominated by start-up, and say what evidence supports it.

Use the profiling tools the task or the run environment provides, such as
`perf` or a sampling profiler for CPU time, an instrumenting profiler for call
counts, and a system-call tracer for syscall counts and gaps. A sampling
profile needs a long enough run to be meaningful; a tracer perturbs timing.
State when a tool is unavailable or a capture changes the timing. This domain
has no accelerator to profile, so do not look for one.

Report the dominant cost center with its share of the measured work, the
invocations it appears in, and the single change the evidence best supports.
The task's own measurement remains the score.
