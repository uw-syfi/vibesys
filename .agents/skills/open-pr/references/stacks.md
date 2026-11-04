# Stacked PRs

Open and maintain a native GitHub stack of dependent PRs.

## Native GitHub Stacks

Use a native GitHub stack when two or more PRs form a true dependency chain in
the same repository. Keep the bottom PR based on the trunk and each later PR
based on the preceding PR's head branch. Do not stack PRs merely because they
are related or opened together.

1. Ensure the official extension is available. Install it when missing:
   `gh extension install github/gh-stack`.
2. Open each PR normally so its title, draft state, and repository-template
   body are deliberate and complete.
3. Link the open PRs in bottom-to-top order:
   `gh stack link --base <trunk> <bottom-pr> <...> <top-pr>`.
   Pass PR numbers or URLs. Omit `--open` unless the user requested ready PRs.
4. Verify each direct base with
   `gh pr view <pr> --json number,baseRefName,headRefName,isDraft,state,url`.
5. Verify remote stack membership with
   `gh api repos/{owner}/{repo}/pulls/<pr> --jq '.stack'`, then inspect the
   reported stack using
   `gh api repos/{owner}/{repo}/stacks/<stack-number>` and confirm its trunk
   and exact bottom-to-top PR order. `gh stack view` reflects local tracking
   and is not sufficient verification after `gh stack link`.

For every intentionally independent PR opened alongside stacked work, target
the trunk, do not pass it to `gh stack link`, and verify that its remote
`.stack` value is `null`.

## Maintaining a stack

PRs land through the GitHub merge queue with squash merge, bottom-up. A child
branch therefore still holds the pre-squash commits of any parent that has
already landed.

- Ask how the stack will be landed (queue order, squash) and which PRs are
  enqueued before touching any stack branch or resolving conflicts across
  branches.
- Never push to a branch that is queued or about to be queued: the push drops
  it from the queue.
- After a parent lands or changes, move the child onto the new parent tip so
  the already-squashed commits are dropped:
  `git rebase --onto <new-parent-tip> <old-parent-tip> <child>`, then
  `git push --force-with-lease`. Do not merge the parent forward into the
  child: that drags pre-squash history in and conflicts with the squashed
  commits on the trunk.
- Force-with-lease on a stack branch is safe only when nobody else pushes to
  it. Ask the user before force-pushing.
- After changing a base PR, check each child against the new base tip with
  `git merge-tree --write-tree <base> <head>` (exit 0 means clean). A child
  that was clean against its old base can conflict with the updated one. Use
  this before trusting or dismissing GitHub's mergeable state, which can lag
  by a minute: poll past `UNKNOWN`.
- Keep child PRs in draft until the stack has been rebased onto the current
  base. Mark a PR ready only after verifying its mergeable state.
