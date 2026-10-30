# Combining in detail

## Why diffs from the built-on commit

Two candidates built on the same parent each carry that parent's code. Applying the second as a
diff from main would undo the first. Applying each as the diff from its own parent adds only
what it changed. When a candidate was built on another candidate, the brief names that parent;
take the diff from it.

If `git apply --3way` leaves a file with markers, edit the file to keep both changes, `git add`
it, and go on. If a part touches code another part deleted, keep the deletion unless the brief
says the part wins.

## What to check after applying

- The build passes and the project's fast tests pass.
- Where two parts interact (the brief's "watch for"), exercise that path.
- Run the task's correctness check through `vibesys:measure`; a combination that fails it is
  not a candidate.

## The lead's decision

For a combine, the result is a new candidate, judged and compared like any other.

For a merge into main, the lead moves main only if the result passes the check and does not
measure worse than main. Concretely, with main at `M` and the result at `C`:

1. `compare --commit C --base M` shows no `worse` verdict. If it is `inconclusive`, take more
   samples of both before deciding.
2. `git -C <main worktree> merge --ff-only C` moves `vibesys/<run-id>` to `C`.
3. The lead records the merge in `decisions.md` and the ledger.

Only one merge runs at a time. Base every merge on the exact SHA of main at the time you
dispatch it; if main moved in the meantime, the lead re-bases the work or starts it again.
