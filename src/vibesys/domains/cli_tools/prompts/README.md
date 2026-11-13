# CLI tools domain prompts

Use for tasks that optimize a locally built native command-line program (C, C++,
Rust, or another systems language) whose observable behavior must be preserved
and whose performance is measured on CPU. Select with
`[agent].domain = "cli-tools"` in the input bundle. The domain is independent of
any particular executable, language, build system, or benchmark framework.

The role files carry the short, always-on guidance: preserve the complete CLI
contract, change one measurable mechanism at a time, and never measure or judge
work that was moved out of the timed region. The task bundle owns the program,
the permitted edit scope, the build, correctness, and measurement commands, and
the scoring rule. The procedural detail lives in the bundled
`cli-tool-optimization` skill, which `skills/domains/.vibesys.toml` selects for
this domain on the CPU backend.

Present:

- `implementer.md`: how to work on the program and what "done" means.
- `judge.md`: the evidence a pass requires and what to reject.
- `orchestrator.md`: how the planner reasons from the workload and profiles.
- `profiler.md`: CPU-appropriate investigation of a representative invocation.

`single_agent.md` is derived from the implementer and judge files. The domain
grants no accelerator profiler; the native CPU profilers apply when the run
environment offers them.
