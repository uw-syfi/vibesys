# Codex

## Locate a skill's own scripts

The skill list Codex shows names each skill with `(file: <path>/SKILL.md)`. The skill's
directory is that file's parent. Its own script: `sh "<directory>/scripts/cli_version.sh"`.
A sibling skill's: `sh "<directory>/../setup/scripts/cli_version.sh"`. Plugin installs
and `npx skills` installs both keep the skills side by side, so the sibling path holds.
