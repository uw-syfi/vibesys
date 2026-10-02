Plan kernel work around the task's reference, candidate contract, checker, and
scored benchmark. Establish a correct baseline before tuning. Write each round
as a testable hypothesis about a measured bottleneck, with explicit correctness
and performance pass criteria.

Useful round boundaries include launch geometry, tiling, memory access,
intermediate storage, synchronization, and arithmetic precision. Prefer one
change at a time so the checker and benchmark can attribute the result.
Schedule profiling when benchmark evidence does not explain the limiting
resource; use the selected profiler's summary to choose the next experiment.
Do not treat a diagnostic capture as a scored result.
