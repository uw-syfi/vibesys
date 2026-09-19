# Triage Signals

Rules for turning `scripts/pr_inventory.sh` output into buckets, owners, and
labels. Apply them in the order written; the first matching bucket wins.

## Contents

- [Signals](#signals)
- [Buckets](#buckets)
- [Who Acts Next](#who-acts-next)
- [Cross-PR Checks](#cross-pr-checks)
- [Review Effort](#review-effort)
- [Review Sequence](#review-sequence)
- [Labels](#labels)
- [Writes](#writes)
- [Report Template](#report-template)

## Signals

| Field | Meaning | Notes |
| --- | --- | --- |
| `mergeable` | GitHub's merge computation | `UNKNOWN` is transient. Re-query with `gh pr view N --json mergeable` after a few seconds; treat persistent `UNKNOWN` as unverified, not as clean. |
| `ci` | Rollup of the head commit's checks | `none` usually means CI has not run for a fork or the head was force-pushed moments ago. |
| `failing_checks` | Names of failed checks | Repository check names: `python-checks` (format, lint, boundaries, doc links), `typecheck`, `tui` (TypeScript lint, boundaries, generated bindings, tests, build), `queue-native`, `test (3.12)`, `sandbox-linux`, `sandbox-macos`, `ci-budget`, `validate-examples`, plus Codecov patch coverage. |
| `waiting_on` | Heuristic owner of the next move | See [Who Acts Next](#who-acts-next). |
| `is_trunk_base` / `stack` | Native stack membership | `stack` is populated only for PRs whose base is not trunk or whose body mentions stacking. Base not trunk with `stack: null` means an unregistered dependency. |
| `template` | PR body headings from `.github/pull_request_template.md` | `unfilled: true` means an HTML comment from the template is still present. |
| `closes` | Issues closed by the PR, with their labels | Populated from GitHub's closing references, not from body text. |
| `touches_prompts` | Any file under `src/vibesys/prompts/` | Needs a CODEOWNERS approval before merge. |
| `size`, `large_files`, `bulk_lines` | Diff size, files with 200 or more changed lines, and lines in generated, vendored, snapshot, fixture, or data paths | Inputs to [Review Effort](#review-effort). Diff size alone is not review effort. |
| `last_other_party_at` | Latest non-author, non-bot comment or review | Compare with `last_commit_at` to see whether the author has responded. |

Staleness uses `updated_at`:

- quiet: no activity for 7 days
- stale: no activity for 21 days
- stale draft: draft with no activity for 30 days

## Buckets

1. `blocked`: base is not trunk and the base PR is still open, or the body
   describes a dependency on another open PR. Nothing to do until the
   predecessor merges; note the predecessor.
2. `needs-author`: any of `ci == failing`, `mergeable == CONFLICTING`,
   review findings or change requests newer than the last commit, missing
   template sections, `unfilled: true`, or a maintainer question without an
   author reply.
3. `draft`: `draft == true` and not otherwise blocked. Report age only. Do not
   review drafts unless the user asks.
4. `stale`: not a draft, no activity for 21 days, and no open review thread
   awaiting the maintainer. Candidate for a ping or close.
5. `ready-to-merge`: not a draft, `ci == passing`, `mergeable == MERGEABLE`,
   base is trunk or its predecessor has merged, an approving review or a
   completed review with no open findings, and a CODEOWNERS approval when
   `touches_prompts` is true.
6. `needs-review`: everything else that is not a draft and has a clean CI and
   merge state. This includes PRs where the author pushed after the last
   review.

Order the report by bucket in the order `ready-to-merge`, `needs-review`,
`needs-author`, `blocked`, `stale`, `draft`. Within a bucket, order by oldest
`updated_at` first so nothing waits indefinitely.

## Who Acts Next

- `author`: draft, failing CI, conflicts, or the last non-bot activity from
  someone else is newer than the last commit.
- `reviewer`: the author pushed after the last review or comment, or nobody
  other than the author has acted yet.

Review findings in this repository are often posted as ordinary comments with
`[P0]` to `[P3]` prefixes rather than formal review objects. Read the latest
maintainer comment when `waiting_on` looks wrong.

## Cross-PR Checks

- Overlapping files: group open PRs by changed path. Two non-stacked PRs from
  different authors touching the same file will conflict; recommend a merge
  order.
- Claimed stacks: a body that says "stacked on" while `is_trunk_base` is true
  means the diff against trunk includes the predecessor's commits. Ask for a
  retarget or a native stack (`gh stack link`), see the `open-pr` skill.
- Shared commits: several trunk-based PRs from one author whose commit lists
  overlap (`gh pr view N --json commits -q '.commits[].oid'`). They are an
  unregistered stack: merging one shrinks or conflicts the others. Report
  the order and ask for independent branches or a native stack.
- Split series: several PRs that close the same issue or share a title
  prefix. Confirm the intended order and that the bottom PR targets trunk.
- Superseded work: an older PR whose scope is covered by a newer merged or
  open PR. Recommend closing with a pointer.
- Large unreviewed PRs: more than 20 changed files without a review. Suggest a
  split or a targeted review plan instead of a full read.

## Review Effort

Estimate the effort to review a PR from its logical change, not its diff
size. Rate each PR `small`, `medium`, or `large`:

- `small`: one logical change a reviewer can hold in their head, typically a
  handful of files and under about 100 lines of hand-written code.
- `medium`: one feature or fix spanning a few modules, or a small change
  whose correctness depends on surrounding code the reviewer must read.
- `large`: several logical changes, a new subsystem, or a change that
  crosses package boundaries (server protocol plus client, loop plus
  evaluator contract).

Discount lines that add no review effort before rating:

- `bulk_lines` counts generated, vendored, snapshot, fixture, and data paths
  (recorded runs, protocol schemas, lockfiles, reference sources).
- A mechanical rename, move, or reformat: confirm the mechanical part with
  `git diff --stat` or a `-M` rename detection, then review only the
  non-mechanical remainder.
- Tests are cheaper to review than the code they cover, but a test file with
  hundreds of lines still needs a skim for what it does not assert.

Note the discount in the report, for example "3655 lines, of which 408 are
the regenerated protocol bindings and about 1200 are tests; logical change is
a new rail view plus its state".

## Review Sequence

End every triage report with a recommended order in which to work through
the reviewable PRs. Build it as follows:

1. Start with `small` PRs in `needs-review` whose `waiting_on` is
   `reviewer`. They clear quickly and unblock authors. Order them by age,
   oldest first.
2. Then the oldest PRs in any bucket that need a maintainer decision:
   a stale PR to close or follow up, a conflicting PR whose author has
   gone quiet, a PR whose linked issue was closed elsewhere. Age counts from
   `created_at`; a PR older than 14 days with no maintainer response needs
   attention regardless of size.
3. Then stacks and dependent chains, bottom-up, so each predecessor merges
   before the next is reviewed. Treat a chain as one queue item and name the
   order.
4. Then `medium` and `large` PRs in `needs-review`, ordered by how many
   other open PRs they unblock (shared files, claimed stacks), then by age.
5. Drafts and PRs in `needs-author` with a clear blocker go last, as a
   watch list rather than review work.

Break ties toward the PR that closes a `bug` issue. Give each entry one
line: PR, effort with any discount, and why it sits there.

## Labels

Apply labels only when the user asks to label. Never remove a label unless
the user names it.

- `bug`: the `Problem` section describes existing incorrect behavior (wrong
  output, crash, inconsistent persisted state, regression, clipped or
  unreadable UI), or a closing issue is labeled `bug` and the PR is the fix.
- `enhancement`: the repository's feature label. Apply to new capability, a
  new surface, new tooling, or a redesign. When the closing issue carries
  both `bug` and `enhancement`, decide from the PR's `Problem` section; a
  redesign that incidentally fixes defects is `enhancement`, a fix that adds
  a field or option on the way is `bug`.
- `refactor`: structural change with no intended behavior change, such as
  extracting a function, deduplicating constants, or moving code between
  owners. A PR that states "no behavior change" in its body is a refactor
  even when its title starts with `fix`.
- `documentation`: docs-only diff.

List borderline cases in the report instead of guessing. Mirror the linked
issue's area labels only if the user asks for full labeling.

## Writes

Every write needs an explicit user request. Commands:

```bash
gh pr edit N --add-label bug
gh pr edit N --add-reviewer login
gh pr comment N --body "text"
gh pr close N --comment "text"
gh pr ready N
```

Project board fields for `uw-syfi/1` need a token with `read:project` and
`project` scopes. If `gh api graphql` returns `INSUFFICIENT_SCOPES`, report
that project state was not checked and continue.

## Report Template

```markdown
## Actions for the maintainer
1. Merge #N (title): reason.
2. Review #N next: reason.
3. Ping @author on #N: what is needed.

## Queue
| PR | Author | Bucket | Blocker | Next action |
| --- | --- | --- | --- | --- |

## Cross-PR notes
- ...

## Recommended review sequence
1. #N (small, 2 files): oldest small bug fix, no reviewer yet.
2. #N (stale, 26 days, conflicting): decide close or follow up.
3. Stack #A then #B then #C: bottom-up, author addressed findings.
4. #N (large diff, small logical change: 400 of 700 lines are a regenerated schema): ...
Watch list: #N (draft), #N (CI failing, author notified).

## Writes made
- Labeled #N, #M with `bug`.
- (or) None.

## Not verified
- Project board state (token lacks read:project).
```
