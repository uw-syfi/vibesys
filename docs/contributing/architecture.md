# Architecture: Python module graph

[`tach.toml`](https://github.com/uw-syfi/vibesys/blob/main/tach.toml) freezes the Python module graph, and CI runs
`uv run tach check`. An import between modules that is not a declared
`depends_on` edge fails the check. Modules cover `src/` (`entrypoints`,
`server.*`, `vibesys.*`) and every `libs/*/src`.

The graphs are generated from `tach.toml` by `tach show --mermaid`. CI fails
when they are stale. To refresh after editing `tach.toml`:

```bash
uv run python scripts/check_tach_graph.py --write
```

- [Full module graph](../figures/tach-module-graph.mmd)
- [Core cycle](../figures/tach-module-graph-core.mmd): the known strongly
  connected core that `tach.toml` tolerates until it is broken.

`.mmd` files do not render inline. Paste one into any Mermaid viewer, or into a
`mermaid` code fence in a Markdown file that supports it.
