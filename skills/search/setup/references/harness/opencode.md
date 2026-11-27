# opencode

## Locate a skill's own scripts

The `skill` tool's output ends with `Base directory for this skill: <directory>`. Its own script:
`sh "<directory>/scripts/cli_version.sh"`. A sibling skill's:
`sh "<directory>/../setup/scripts/cli_version.sh"`. Skills installed or copied side by side
keep that sibling path valid.
