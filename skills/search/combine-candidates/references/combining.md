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

Promotion into main is the lead's decision (see the optimize skill): it needs an independent
judge pass and a recorded `better` comparison against current main, and a result within noise
cannot move main. If main moved since dispatch, measure against its current head before
retrying promotion.
