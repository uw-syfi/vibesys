# Message schema

`commit_schema.py check-message FILE` and `check-range BASE..HEAD` apply these rules. Exit 0 is
valid, 1 is violations (printed as `commit: rule: problem Fix: how`), 2 is unusable input.

## Full commit

| Rule | Meaning |
| --- | --- |
| `header-format`, `type-unknown` | First line is `<type>(<scope>): <subject>`; type is lowercase and one of the list in SKILL.md. Scope is optional: letters, digits, `.`, `_`, `/`, `-`. `!` after the type marks a breaking change. |
| `subject-empty`, `subject-period`, `subject-length` | The subject is not empty, has no final period, and the whole first line is at most 72 characters. |
| `subject-imperative` | The first word does not read as past tense (`added`), a gerund (`adding`) or third person (`adds`). A heuristic on word endings; reword the subject to fix it. |
| `blank-after-subject` | One blank line between the first line and the rest. |
| `body-line-length` | Body and trailer lines are at most 72 characters. Lines with a URL, indented code (4 spaces or a tab) and fenced code are exempt. |
| `checks-missing`, `checks-value`, `checks-duplicate` | Exactly one `Checks:` trailer, `pass`, `fail` or `not-run`. |
| `plan-missing`, `plan-mismatch`, `plan-value` | On `vibesys-work/<task>/plan-NNN`, one `Plan: NNN` that equals the branch number. Elsewhere `Plan:` is optional but must be one number. |
| `encoding`, `message-empty` | UTF-8 without NUL bytes, and not empty. |

Trailers follow `git interpret-trailers`: they are the last paragraph, after a blank line, and
the first paragraph is never a trailer block. A `Checks: pass` line inside a body paragraph is not
a trailer. In `check-message` (the hook), Git's `#` comment lines are ignored.

Length and shape are enforced; a good body is not. Say why the change is right, what you tried,
and what a reader needs to trust it. A mismatch of `Checks` with the truth is the worst error.

## wip commit

`wip: <note>` or `wip(<scope>): <note>`. Only that first line and a non-empty note are checked
(`header-format`, `wip-note-empty`). No trailers, no length or imperative rule. It means checks
not run or unknown. It is never a candidate: `wip-not-measurable` (from `--require-pass` and for the head in
`check-range`) says to commit a full commit on top.

## Examples

Good:

```
perf(decode): fuse the sampling kernel into the logits epilogue

Sampling re-read the logits from global memory; profile 4 showed it at 11%
of step time. Fusing removes the read.

Checks: pass
Plan: 007
```

```
wip(scheduler): try batching by prompt length
```

Bad:

| Message | Rule | Fix |
| --- | --- | --- |
| `Added caching.` | `header-format` | `perf(cache): add a result cache` |
| `feat: adds a cache.` | `subject-imperative`, `subject-period` | `feat: add a cache` |
| `fix: handle empty input` with no trailer | `checks-missing` | add `Checks: pass` after a blank line |
| `Feature: x` | `header-format` | use a listed lowercase type |
| `Plan: 3` on branch `.../plan-004` | `plan-mismatch` | `Plan: 004` |
| `wip:` | `wip-note-empty` | `wip: half-done tiling` |
| `Checks: not-run` on the line right after the subject | `blank-after-subject` | add a blank line |
