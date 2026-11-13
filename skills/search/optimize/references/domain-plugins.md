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
| `domain = "fpga-hls"` | `vibesys-fpga-hls` | `fpga-hls-workflow` |

A `llm-serving` task on backend `trainium` needs both `vibesys-llm-serving` and
`vibesys-kernels-trainium`. Any other domain or backend (for example `generic` on `cpu`) needs
no domain plugin.

A skill is installed when it appears among the skills your harness offers you, by its plugin
name (`vibesys-kernels-cuda:kernel-performance`) or, where the harness has no plugin prefix, its
bare name. It is also available when the user or the operator's instructions name a directory
that holds it (such as a vibesys checkout's `skills/domains/<skill>/SKILL.md`): then point every
brief at that file by its absolute path instead of the plugin name, so a fix to the skill reaches
the next subagent. Otherwise do not search the filesystem for it. When one is missing, stop and tell the user to
run, for Claude Code:

```
claude plugin marketplace add uw-syfi/vibesys
claude plugin install <plugin>@vibesys
```

or, for Codex, `codex plugin marketplace add uw-syfi/vibesys` then
`codex plugin add <plugin>@vibesys`, and then to start you again.
