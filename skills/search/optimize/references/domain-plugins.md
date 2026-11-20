# Domain plugins

The domain skills ship in their own plugins. Read `[agent] domain` and `backends` in
`vibesys.input.toml` and find the plugins the task needs:

| Task | Plugin | Skill to look for |
| --- | --- | --- |
| `domain = "llm-serving"` | `vibesys-llm-serving` | `serving-systems` |
| `domain = "microservices"` | `vibesys-microservices` | `microservice-optimization` |
| `domain = "database"` | `vibesys-database` | `dial9-runtime` |
| `domain = "kernel-writing"`, backend `cuda` | `vibesys-kernels-cuda` | `kernel-performance` |
| `domain = "kernel-writing"`, backend `rocm` | `vibesys-kernels-rocm` | `kernel-performance` |
| `domain = "kernel-writing"`, backend `trainium` | `vibesys-kernels-trainium` | `kernel-performance` |
| `domain = "kernel-writing"`, any other backend | any kernels plugin, such as `vibesys-kernels-cuda` | `kernel-performance` |
| `domain = "cli-tools"` | `vibesys-cli-tools` | `cli-tool-optimization` |

`vibesys-llm-serving` also holds `serving-studio`, the ServingStudio simulator's predictions. It
is optional: a task runs without it, and it needs a simulator set up on the host (its Setup
section). Name it in planner and profiler briefs, not in worker, combiner or judge briefs.

A `cli-tools` task targets the `cpu` backend, so it needs `vibesys-cli-tools` and no backend
plugin. A `llm-serving` task on backend `trainium` needs both `vibesys-llm-serving` and
`vibesys-kernels-trainium`, and on backend `rocm` both `vibesys-llm-serving` and
`vibesys-kernels-rocm`, because `serving-systems` captures ROCm profiles with the
`kernel-rocprof-analysis` skill. Any task on backend `trainium` also needs the `neuron-safeguards`
skill of `vibesys-kernels-trainium`, whatever its domain: its checklist goes into every implement
and combine brief. A `kernel-writing` task on a backend with no kernels plugin of its
own (`cpu`, `metal`) still uses the backend-neutral `kernel-correctness` and `kernel-performance`,
which every kernels plugin lists: name only those two in briefs, since the plugin's analysis skills
are for its own backend. `generic` on any backend, and a listed domain on a backend the table does
not pair it with, need no other domain plugin. A domain that appears nowhere in this table and is not
`generic` is one this plugin version does not know: carry on, and say in your first report that no
domain skill was found for it.

A skill is installed when it appears among the skills your harness offers you, by its plugin
name (`vibesys-kernels-cuda:kernel-performance`) or, where the harness has no plugin prefix, its
bare name. It is also available when the user or the operator's instructions name a directory
that holds it (such as a vibesys checkout's `skills/domains/<skill>/SKILL.md`; the skills of the
two vendored packs sit one folder further down: `dial9-tokio/skills/<skill>/SKILL.md`, and
`dial9-tokio/dial9-rank-bottlenecks/SKILL.md` for the workflow skill, and
`neuron-agentic-development/skills/<skill>/SKILL.md`, and
`neuron-agentic-development/neuron-safeguards/SKILL.md` for the safeguards skill): then point every
brief at that file by its absolute path instead of the plugin name, so a fix to the skill reaches
the next subagent. Otherwise do not search the filesystem for it.

In a run the orchestrator started (`VIBESYS_RUN` is set), the session loads only the `vibesys`
plugin and no other plugin can be installed into it, so the install commands below cannot help.
When a skill is missing there, send a `final` report whose headline names the missing skill and
says the operator must start the run again with a brief that names its `SKILL.md` by absolute
path (a release keeps it under `marketplace/skills/domains/`), and start nothing else.

Otherwise, when one is missing, stop and tell the user to run, for Claude Code:

```
claude plugin marketplace add uw-syfi/vibesys
claude plugin install <plugin>@vibesys
```

or, for Codex, `codex plugin marketplace add uw-syfi/vibesys` then
`codex plugin add <plugin>@vibesys`, and then to start you again.
