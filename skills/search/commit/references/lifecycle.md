# Lifecycle: enforcement, measurement and promotion

## Where the rules are enforced

1. **The hook** (`install-hook`) rejects a bad message at commit time. It accepts `wip`, so a
   checkpoint is never blocked. It can be skipped with `--no-verify`, so it is a convenience.
2. **Measurement** is the gate that cannot be skipped. `measurements.py record --role
   candidate` reads the commit's message and refuses it if it violates the schema, is `wip`, or
   has a `Checks` other than `pass`. It names the fix. The base commit is exempt: it is the
   repository's own history. A `record` run without this skill installed beside `measure` fails
   with a message naming the missing script; it never passes silently.
3. **Promotion**: `measurements.py check` runs the range check over every commit from the base to
   the result branch head and lists schema violations, so a commit reaching the result branch
   without passing `record` still shows.

## What lands on the result branch

Frequent small commits are kept as they are. Promotion is a fast-forward of the verified
candidate head, so every commit between the candidate's parent and its head lands, `wip`
checkpoints included. Only the head is measured and judged. The judge reviews the candidate's
whole diff from its parent (`git diff <parent> <candidate>`), never one commit, so the number of
commits does not change what is judged. Nobody squashes: that would change the measured SHA.

A measured commit is never amended, rebased or force-pushed, because records are keyed by SHA.
Before measurement, history tidying is allowed and unnecessary.

## Why the hook is per-worktree

`install-hook` must not clobber a hook the repository has and must not touch the user's other
work. Options, scored out of 5 for (safe for the user's own commits, keeps existing hooks,
scoped to the optimization, survives git versions):

| Option | Score | Verdict |
| --- | --- | --- |
| Write `.git/hooks/commit-msg` | 1, 1, 1, 5 | Replaces the user's hook and fires on all their commits. |
| Repository `core.hooksPath` | 1, 2, 1, 5 | Hides every hook in the old directory from everyone. |
| Per-worktree `core.hooksPath` (`extensions.worktreeConfig`), directory in the state directory, forwarding the other hooks | 4, 5, 5, 4 | Chosen. Only the optimization's worktrees change. |
| Environment variables on each `git` call | 5, 5, 5, 1 | Cannot reach agents' own shells. |

The chosen option sets one repository key (`extensions.worktreeConfig`), which does not affect
the main checkout, and refuses repositories with `core.bare` or `core.worktree`. Git 2.20 or newer.
The generated directory forwards every other hook of the previous directory and chains to its
`commit-msg`. Re-run `install-hook` after a new worktree or after the repository adds a hook.
