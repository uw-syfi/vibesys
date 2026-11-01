# Codex

## Locate a skill's own scripts

The skill list Codex shows names each skill with `(file: <path>/SKILL.md)`. The skill's
directory is that file's parent. Its own script: `python3 "<directory>/scripts/measurements.py"`.
A sibling skill's: `python3 "<directory>/../measure/scripts/measurements.py"`. Plugin installs
and `npx skills` installs both keep the skills side by side, so the sibling path holds.

