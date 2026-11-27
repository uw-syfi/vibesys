# opencode

## Locate a skill's own scripts

The `skill` tool's output ends with `Base directory for this skill: <directory>`. Analyze a
copied-back capture with `python3 "<directory>/scripts/analyze.py" <group> ...`. Copy the
capture wrapper into the candidate repository with
`cp "<directory>/scripts/capture.py" tools/capture.py`, then commit it.
