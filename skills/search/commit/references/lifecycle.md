# Lifecycle: enforcement, measurement and promotion

## Where the rules are enforced

1. **The hook** (`install-hook`) rejects a bad message at commit time. It accepts `wip`, so a
   checkpoint is never blocked. It can be skipped with `--no-verify`, so it is a convenience.
2. **The range check** is the gate that cannot be skipped by `--no-verify`. `vibesys-cli` measures
   any commit and does not read its message, so the lead runs `commit_schema.py check-range
   <base>..<candidate>` before it submits a measure, and before it promotes. The range check
   refuses a schema violation, a `wip` head, and a `Checks` other than `pass`, and names the fix.
   The base commit is exempt: it is the repository's own history.

## What lands on the result branch

Frequent small commits are kept as they are. Promotion is a fast-forward of the verified
candidate head, so every commit between the candidate's parent and its head lands, `wip`
checkpoints included. Only the head is measured and judged. The judge reviews the candidate's
whole diff from its parent (`git diff <parent> <candidate>`), never one commit, so the number of
commits does not change what is judged. Nobody squashes: that would change the measured SHA.

A measured commit is never amended, rebased or force-pushed, because results are keyed by SHA.
Before measurement, history tidying is allowed and unnecessary.

## Why the hook is per-worktree

`install-hook` must not clobber a hook the repository has and must not touch the user's other
work. Options, scored out of 5 for (safe for the user's own commits, keeps existing hooks,
scoped to the optimization, survives git versions):

| Option | Score | Verdict |
| --- | --- | --- |
| Write `.git/hooks/commit-msg` | 1, 1, 1, 5 | Replaces the user's hook and fires on all their commits. |
| Repository `core.hooksPath` | 1, 2, 1, 5 | Hides every hook in the old directory from everyone. |
| Per-worktree `core.hooksPath` (`extensions.worktreeConfig`), directory inside the Git directory, forwarding the other hooks | 4, 5, 5, 4 | Chosen. Only the optimization's worktrees change. |
| Environment variables on each `git` call | 5, 5, 5, 1 | Cannot reach agents' own shells. |

The chosen option sets one repository key (`extensions.worktreeConfig`), which does not affect
the main checkout, and refuses repositories with `core.bare` or `core.worktree`. Git 2.20 or newer.
The generated directory forwards every other hook of the previous directory and chains to its
`commit-msg`. Re-run `install-hook` after a new worktree or after the repository adds a hook.
