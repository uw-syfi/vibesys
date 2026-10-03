Review the candidate as a compute-kernel implementation. A pass requires the
task's full correctness contract and its measured performance criteria.

Check the allowed edit surface and confirm the candidate computes outputs from
runtime inputs. Reject hard-coded benchmark answers, input-dependent bypasses,
changes to the reference or evaluator, and work moved outside the timed path
when the contract requires it inside.

Use the task's accuracy evidence to check all required shapes, dtypes, layouts,
and numerical tolerances, including boundary cases. Treat nondeterminism,
uninitialized reads, data races, and out-of-bounds accesses as correctness
failures where the contract prohibits them. A pass on one measured shape does
not establish correctness across the supported set.

Judge speed using the task's benchmark and stated scoring metric only after
correctness passes. Keep diagnostic profiler numbers separate from scored
measurements. If a claimed improvement lacks comparable before and after runs,
record that limit in the verdict.
