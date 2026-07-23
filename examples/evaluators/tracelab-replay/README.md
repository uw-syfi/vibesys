# TraceLab Replay Evaluator

This evaluator is intended for `[hidden_evaluator]` use only. VibeSys copies it
outside the candidate workspace and injects its path only for framework-owned
accuracy/benchmark execution.

The benchmark uses TraceLab's own Rust `session_runner` and the pinned public
TraceLab `v0.0.1` DuckDB release. The visible input bundle contains only a thin
shim so optimization agents can learn the benchmark command shape without
seeing the runner implementation or collected trace data.
