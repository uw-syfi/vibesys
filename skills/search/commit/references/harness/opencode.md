# opencode

## Locate a skill's own scripts

The `skill` tool's output ends with `Base directory for this skill: <directory>`. Its own script:
`python3 "<directory>/scripts/commit_schema.py"`. A sibling skill's:
`python3 "<directory>/../commit/scripts/commit_schema.py"`. Skills installed or copied side by side
keep that sibling path valid.
