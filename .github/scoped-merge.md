# Scoped TUI and server merges

The `Scoped TUI and server merge` workflow lets selected collaborators merge a
pull request by commenting `/merge-tui` without granting them repository-wide
write access. The workflow runs trusted code from the default branch and uses
the job's short-lived `GITHUB_TOKEN` to perform the merge.

## GitHub configuration

1. Configure the repository Actions variable `SCOPED_MERGE_USERS` as a comma-
   or whitespace-separated list of GitHub logins.
2. Disable **Restrict who can push to matching branches** in the `main` branch
   protection rule. The built-in Actions identity cannot be added to that list.
   Delegated maintainers remain unable to push because they receive Triage,
   not Write, access.
3. Require pull requests for `main`, then require the `Scoped merge gate`
   status check and conversation resolution.
   Leave GitHub's required approval count at zero. The scoped flow intentionally
   does not require a human review.
4. Give each login in `SCOPED_MERGE_USERS` the repository `Triage` role, not
   `Write` or `Maintain`.

The workflow refuses a merge unless the PR targets `main`, is clean and
mergeable, the command issuer still has at least `Triage` repository access, and
the latest `test.yml` run for that commit succeeded. It validates the complete
changed-file list, including both paths of a rename, against
`scoped-merge.toml`, then passes the validated head SHA to GitHub's merge API.

GitHub suppresses most workflow events caused by `GITHUB_TOKEN`. The PR test
workflow has already passed before the merge, but the resulting update to
`main` does not start workflows configured only for `push`.

Missing configuration fails closed. When revoking authority, remove the
collaborator's repository access before removing their login from the variable.
The live access check then also rejects commands that were already queued.
