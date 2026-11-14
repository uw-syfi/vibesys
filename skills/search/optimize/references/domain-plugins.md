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
| `domain = "cli-tools"` | `vibesys-cli-tools` | `cli-tool-optimization` |

A `cli-tools` task targets the `cpu` backend, so it needs `vibesys-cli-tools` and no backend plugin.
A `llm-serving` task on backend `trainium` needs both `vibesys-llm-serving` and
`vibesys-kernels-trainium`. `generic` on any backend, and a listed domain on a backend the table
does not pair it with, need no domain plugin. A domain that appears nowhere in this table and is
not `generic` is one this plugin version does not know: carry on, and say in your first report
that no domain skill was found for it.

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
