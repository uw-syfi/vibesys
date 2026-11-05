# Codex

## Locate a skill's own scripts

The skill list Codex shows names each skill with `(file: <path>/SKILL.md)`. The skill's
directory is that file's parent. Analyze a copied-back capture with
`python3 "<directory>/scripts/analyze.py" <group> ...`. Copy the capture wrapper into the
candidate repository with `cp "<directory>/scripts/capture.py" tools/capture.py`, then commit it.
