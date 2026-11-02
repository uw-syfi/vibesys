---
name: commit
description: "Use when you commit work in a vibesys optimization: at every working step, before any measurement, and before ending a turn. Defines when to commit, what a commit holds, the enforced message schema (Conventional Commits plus Checks and Plan trailers, with cheap wip checkpoints), and which commits may be measured or promoted. Never touch .vibesys/, the task's checker or its benchmark."
---

# Commit

Commit often and in small steps. A commit is a checkpoint of your work and of your thinking, and
a measurement is always of a commit. Message rules are enforced by `commit_schema.py` in this
skill's `scripts/` directory (standard library, Python 3.10+). Details and examples:
[message.md](references/message.md). Why the checks sit where they do, and what reaches the
result branch: [lifecycle.md](references/lifecycle.md).

Script paths: read `references/harness/<harness>.md` for the harness you are running in.

## When to commit

- At every working step, and before you try something else. Do not wait for green checks.
- Before any measurement and before you end a turn. Uncommitted work is lost to the lead.
- A checkpoint costs one line: `git commit -am "wip: <note>"` (stage new files first). Use it
  for work in progress, a half idea or a state you may want back. A `wip` commit needs no
  trailers, no length limit and no passing checks.

## What a commit holds

- One logical change. Do not mix a refactor with a behavior change or reformat unrelated code.
- Everything a clean checkout needs to build, check and benchmark the change: source, build
  config, generated or untracked inputs, and anything gitignored that the benchmark reads
  (force-add it with `git add -f`). A measurement runs a commit hash in a clean checkout and
  never your working copy, so what is not committed is not measured.
- No build output, caches, large artifacts or secrets.

## Never touch

`.vibesys/`, the task's checker and its benchmark. Do not commit changes to them.

## Message

```
<type>(<optional scope>): <imperative subject, at most 72 characters, no final period>

<optional body: why, wrapped at 72; URLs and code may be longer>

Checks: pass|fail|not-run
Plan: NNN
```

Types: `feat fix perf refactor test docs build chore revert`, plus `wip`. `Checks` is required on
every full commit: say `pass` only for checks you ran on this commit. `Plan: NNN` is required on
a `vibesys-work/<task>/plan-NNN` branch and must match the branch number.

Check before you finish: `python3 <this skill>/scripts/commit_schema.py check-range <base>..HEAD`.
Each violation names the commit, the rule and the fix.

## Measured commits are history

The commit you hand to measurement must be a full commit with `Checks: pass`, never `wip`.
`vibesys-cli` measures any commit, so the lead runs `check-range <base>..<sha>` before submitting
a candidate. Never amend, rebase or force-push a commit that
has been measured; results are keyed by its SHA. To change it, commit on top. Before measurement,
you may tidy your history, but you never have to: `wip` commits below the candidate head stay.

## Hook

`commit_schema.py install-hook --worktree <path>` adds a `commit-msg` hook to the given
worktrees and keeps any hook the repository has. The lead runs it after creating each worktree
(`vibesys:state`). `git commit --no-verify` skips the hook; the range check before measurement
does not.
