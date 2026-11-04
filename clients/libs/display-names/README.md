# VibeSys display names

`@vibesys/display-names` holds the names a person reads for VibeSys's own
vocabularies: an agent role id (`perf-evaluator`) reads as its title ("Perf
evaluator"). The catalog in `src/generated` is generated from
`vibesys.api.display_catalog`, the one place the role titles are declared, so no
screen maps an id itself. Regenerate it with `pnpm generate`; a Python test
fails when the file differs from what the catalog says.

It is runtime neutral (no DOM, no Node) and depends on no other package. The
`no-role-words` Biome rule and the screen checks read the same file.
