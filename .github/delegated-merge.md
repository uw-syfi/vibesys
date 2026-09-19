# Delegated merges

The `Delegated merge` workflow lets selected collaborators merge pull requests
by commenting `/merge-scoped` without granting repository-wide write access.
The workflow runs trusted code from the default branch and uses the job's
short-lived `GITHUB_TOKEN` to merge or enqueue.

## Policy

`.github/delegated-merge.toml` owns the repository, base branch, merge method,
named required checks, and capability-indexed path policy. Each
`[capabilities.<name>]` section lists case-insensitive GitHub login members,
repository path prefixes, exact paths, and optional additional checks. Matching
a section implicitly requires membership in that capability. Every changed
path must match at least one section. For renames, both the old and new path are
evaluated. When a path or diff matches several capability sections, the broker
requires the caller to be a member of all matching capabilities and requires
the union of their checks. Repository administrators bypass only capability
membership. They must still issue the exact command and pass the path, rename,
named-CI, PR-state, base, cleanliness, live-role, and head-SHA checks. Empty
member lists are valid and grant no non-admin user access.

Membership is source-controlled and changes through the normal maintainer path.
For example:

```toml
[capabilities.tui]
members = ["alice"]
prefixes = ["clients/tui/src/"]
paths = []
additional_checks = []
```

The broker's policy, workflow, implementation, and configured check workflow
paths are always denied, even if a future capability section would otherwise
match them.

Named checks point to a workflow file and an exact job name. The broker selects
the latest `pull_request` workflow run for the PR's exact head SHA, then requires
exactly one job with that name and requires that job to have completed
successfully. It does not rely on the workflow's aggregate conclusion.

## GitHub configuration

1. Add delegated maintainers to the appropriate capability `members` arrays in
   `.github/delegated-merge.toml` through a normal maintainer pull request.
2. Disable **Restrict who can push to matching branches** in the `main` branch
   protection rule. The built-in Actions identity cannot be added to that list.
   Delegated maintainers remain unable to push because they receive Triage,
   not Write, access.
3. Require pull requests for `main`, then require `Required PR CI` and
   conversation resolution. Leave GitHub's required approval count at zero.
   The delegated flow intentionally does not require a human review.
4. Give each listed member the repository `Triage` role, not `Write` or
   `Maintain`.

The workflow also requires the PR to target `main`, be clean and mergeable, and
the command issuer to retain at least Triage access. The complete file list is
validated before the broker passes the validated head SHA to GitHub's merge
API. Missing or malformed policy fails closed.

### Direct merge and merge queue

All scope and policy checks are independent of how the pull request lands.
After they pass, one of three landing strategies runs, chosen by a GraphQL read of
`repository.mergeQueue(branch: "main")` and the `stack` field of the pull
request REST object:

- No merge queue on `main`: `DirectMerge` calls the pull request merge API with
  the validated head SHA. The comment reads ``Scoped merge completed at `<sha>` ``.
- Merge queue on `main`: `QueueEnqueue` calls the `enqueuePullRequest` GraphQL
  mutation with the validated head SHA as `expectedHeadOid`. The comment reads
  `Scoped merge enqueued: ...` and states that nothing is merged yet. The
  queue's own `merge_group` CI is the final gate and performs the merge. No
  merge SHA is reported for an enqueue.
- Merge queue on `main` and the pull request is in a native GitHub stack:
  GitHub rejects `enqueuePullRequest` for stack members and requires the
  [asynchronous merge API](https://docs.github.com/rest/pulls/pulls#merge-a-pull-request-asynchronously),
  so `StackedQueueEnqueue` sends `PUT /repos/{owner}/{repo}/pulls/{n}/merge-async`
  with `sha` (the validated head), `merge_method`, and `merge_action:
  merge_queue`. A `pending` answer means the request was accepted and runs in
  the background; the comment is the same `Scoped merge enqueued: ...` text. The
  answer must echo the validated head as `expected_head_sha`, otherwise the
  command refuses.

That API merges every pull request in the stack up to and including the
requested one, and only the requested pull request is scope-validated. The
command therefore refuses stacked pull requests that are not at position 1
(bottom), that have no readable position, or whose base branch has no merge
queue. Land the lower pull requests first. A pull request without a `stack`
object is unstacked and uses the first two strategies.

A repeated `/merge-scoped` on a pull request already in the queue (or reported
`enqueued` by the async API) posts no comment and changes nothing. If the queue state cannot be read or parsed, the
command refuses and neither merges nor enqueues. If the queue is enabled after
the state was read, the direct merge is refused by GitHub and reported as a
refusal, never as success. The `enqueuePullRequest` mutation needs
`pull-requests: write`, so the workflow grants it to the job token. Whether
`GITHUB_TOKEN` is accepted for enqueueing has not been exercised against a real
queue; if GitHub rejects it, the command refuses safely and a dedicated token
would be needed.

The async endpoint has not been exercised against a real stacked pull request:
the shape above follows GitHub's REST description. Confirm with a stack whose
bottom pull request is in scope.

To verify after enabling the queue with group size 1, comment `/merge-scoped`
on a low-risk in-scope pull request and confirm it is enqueued and then merged
by the queue.

GitHub suppresses most workflow events caused by `GITHUB_TOKEN`. The PR test
workflow has already passed before the merge, but the resulting update to
`main` does not start workflows configured only for `push`.

## Setup

GitHub does not start workflows for events created with `GITHUB_TOKEN`. A merge
queue entry created with it never gets its `merge_group` CI run, so the entry
stalls. The landing writes (`enqueuePullRequest`, `PUT .../merge-async`, and the
direct `PUT .../merge`) therefore use a fine-grained personal access token
passed to the `Authorize and merge or enqueue` step as `LANDING_GH_TOKEN`. Reads,
the role check, and audit comments keep `GITHUB_TOKEN`.

1. Create a fine-grained PAT owned by a maintainer account (ideally a dedicated
   machine user), with resource owner `uw-syfi`, repository access limited to
   `vibesys`, and repository permissions Pull requests read and write and
   Contents read and write (Metadata read is implied). Leave every other
   permission unset. Set an expiration.
2. Add it as the repository secret `MERGE_QUEUE_PAT`.

To rotate, generate a new PAT, replace `MERGE_QUEUE_PAT`, run `/merge-scoped` on
a small in-scope pull request to confirm, then revoke the old PAT.

If `MERGE_QUEUE_PAT` is missing or empty, the script refuses with a message
naming the landing token, before any landing write. It never falls back to
`GITHUB_TOKEN`.

## Migration from the scoped TUI bot

1. Land this change while the existing `Scoped merge gate` branch-protection
   requirement remains enabled. `test.yml` temporarily reports that name as a
   compatibility alias for `Required PR CI`.
2. Wait for `Required PR CI` to complete successfully on `main` so GitHub makes
   it available as a required status check.
3. Add `Required PR CI` to the `main` protection rule, then remove
   `Scoped merge gate` from the rule.
4. Add each delegated maintainer to the `tui`, `server`, or both capability
   member lists in `.github/delegated-merge.toml` through the normal maintainer
   path. Delete the obsolete `SCOPED_MERGE_USERS` repository variable.
5. Tell maintainers to use `/merge-scoped`. The old command is not accepted.
6. After branch protection no longer refers to `Scoped merge gate`, remove the
   temporary compatibility job in a follow-up change.

When revoking authority urgently, remove the collaborator's repository access
before landing the policy change that removes their membership. The live access
check also rejects commands that were already queued.
