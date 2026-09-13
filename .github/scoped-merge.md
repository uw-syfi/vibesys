# Scoped TUI and server merges

The `Scoped TUI and server merge` workflow lets selected collaborators merge a
pull request by commenting `/merge-tui` without granting them repository-wide
write access. The workflow runs trusted code from the default branch and uses a
dedicated GitHub App to perform the merge.

## GitHub configuration

1. Create a GitHub App with these repository permissions:
   - Actions: read
   - Contents: read and write
   - Issues: read and write
   - Pull requests: read
2. Install it only on `uw-syfi/vibesys`.
3. Configure these repository Actions values:
   - Variable `SCOPED_MERGE_APP_ID`: the App ID.
   - Secret `SCOPED_MERGE_APP_PRIVATE_KEY`: one private key generated for the App.
   - Variable `SCOPED_MERGE_USERS`: comma- or whitespace-separated GitHub logins.
4. In the `main` branch protection rule, add the App under **Restrict who can
   push to matching branches**. Do not make the App or delegated users bypass
   actors.
5. Require the `Scoped merge gate` status check and conversation resolution.
   Leave GitHub's required approval count at zero. The scoped flow intentionally
   does not require a human review.
6. Give each login in `SCOPED_MERGE_USERS` the repository `Triage` role, not
   `Write` or `Maintain`.

The workflow refuses a merge unless the PR targets `main`, is clean and
mergeable, the command issuer still has at least `Triage` repository access, and
the latest `test.yml` run for that commit succeeded. It validates the complete
changed-file list, including both paths of a rename, against
`scoped-merge.toml`, then passes the validated head SHA to GitHub's merge API.

Missing configuration fails closed. When revoking authority, remove the
collaborator's repository access before removing their login from the variable.
The live access check then also rejects commands that were already queued.
