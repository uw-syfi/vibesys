# Delegated merges

The `Delegated merge` workflow lets selected collaborators merge pull requests
by commenting `/merge-scoped` without granting repository-wide write access.
The workflow runs trusted code from the default branch and uses the job's
short-lived `GITHUB_TOKEN` to perform the merge.

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

GitHub suppresses most workflow events caused by `GITHUB_TOKEN`. The PR test
workflow has already passed before the merge, but the resulting update to
`main` does not start workflows configured only for `push`.

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
