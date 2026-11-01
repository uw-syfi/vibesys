# Harness mappings

The search skills are written against abstract capabilities, not one agent product. This folder
maps those capabilities to a concrete agent harness: one file per provider (`claude-code.md`),
all with the same headings:

1. Dispatch a subagent with a fresh context
2. Run a long command as a tracked background task
3. Locate a skill's own scripts
4. Schedule periodic work
5. Wait for work without polling
6. Packaging

To add a provider, copy an existing file to `<provider>.md`, keep the headings, and fill each
with that harness's mechanism, or say it has none. Provider names and tool names appear only in
this folder.
