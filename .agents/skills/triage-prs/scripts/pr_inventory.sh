#!/usr/bin/env bash
# Emit one JSON object per open pull request carrying the signals the
# triage-prs skill classifies on. Read-only. Requires gh and jq.
#
# Usage: pr_inventory.sh [--repo owner/name] [--no-stacks]
#
# Fields per line (all timestamps ISO 8601):
#   number, title, url, author, draft, labels, base, head, is_trunk_base,
#   mergeable (MERGEABLE|CONFLICTING|UNKNOWN), review_decision,
#   review_requests, created_at, updated_at, last_commit_at,
#   last_other_party, last_other_party_at, waiting_on (author|reviewer),
#   ci (passing|failing|pending|none), failing_checks, size,
#   files (first 100 paths), large_files (>= 200 changed lines),
#   bulk_lines (changed lines in generated, vendored, snapshot, fixture, or
#   data paths, which inflate the diff without adding review effort),
#   template {problem, solution, verification, unfilled},
#   closes [{number, labels}], mentions_stack,
#   stack (native GitHub stack {id, position, size, base} or null).
set -euo pipefail

repo=""
stacks=1
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    --no-stacks) stacks=0; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$repo" ]; then
  repo="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
fi
owner="${repo%/*}"
name="${repo#*/}"
default_branch="$(gh api "repos/$repo" --jq .default_branch)"

read -r -d '' query <<'GQL' || true
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $endCursor,
                 orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft createdAt updatedAt body
        author { login }
        labels(first: 20) { nodes { name } }
        baseRefName headRefName mergeable reviewDecision
        reviewRequests(first: 10) {
          nodes { requestedReviewer { ... on User { login } ... on Team { name } } }
        }
        reviews(last: 30) { nodes { author { login } state submittedAt } }
        comments(last: 30) { nodes { author { login } createdAt } }
        additions deletions changedFiles
        files(first: 100) { nodes { path additions deletions } }
        closingIssuesReferences(first: 10) {
          nodes { number labels(first: 20) { nodes { name } } }
        }
        commits(last: 1) {
          nodes {
            commit {
              committedDate
              statusCheckRollup {
                contexts(first: 60) {
                  nodes {
                    __typename
                    ... on CheckRun { name conclusion status }
                    ... on StatusContext { context state }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
GQL

emit() {
  gh api graphql --paginate -F owner="$owner" -F name="$name" -f query="$query" \
  | jq -c --arg trunk "$default_branch" '
    def is_bot: (. // "") | test("\\[bot\\]$|^codecov|^github-actions");
    .data.repository.pullRequests.nodes[]
    | . as $pr
    | ($pr.commits.nodes[0].commit) as $commit
    | ([$commit.statusCheckRollup.contexts.nodes[]?
        | if .__typename == "CheckRun"
          then {name: .name, result: .conclusion, done: (.status == "COMPLETED")}
          else {name: .context, result: .state, done: (.state != "PENDING" and .state != "EXPECTED")}
          end]) as $checks
    | ([$pr.comments.nodes[] | {login: .author.login, at: .createdAt}]
       + [$pr.reviews.nodes[] | {login: .author.login, at: .submittedAt}]
       | map(select(.login != $pr.author.login and (.login | is_bot | not)))
       | max_by(.at)) as $other
    | ($commit.committedDate) as $last_commit_at
    | (if ($checks | length) == 0 then "none"
       elif any($checks[]; .result == "FAILURE" or .result == "ERROR"
                           or .result == "TIMED_OUT" or .result == "CANCELLED") then "failing"
       elif any($checks[]; .done | not) then "pending"
       else "passing" end) as $ci
    | {
        number: $pr.number,
        title: $pr.title,
        url: $pr.url,
        author: $pr.author.login,
        draft: $pr.isDraft,
        labels: [$pr.labels.nodes[].name],
        base: $pr.baseRefName,
        head: $pr.headRefName,
        is_trunk_base: ($pr.baseRefName == $trunk),
        mergeable: $pr.mergeable,
        review_decision: $pr.reviewDecision,
        review_requests: [$pr.reviewRequests.nodes[].requestedReviewer | (.login // .name)],
        created_at: $pr.createdAt,
        updated_at: $pr.updatedAt,
        last_commit_at: $last_commit_at,
        last_other_party: ($other.login // null),
        last_other_party_at: ($other.at // null),
        waiting_on: (if $pr.isDraft or $ci == "failing" or $pr.mergeable == "CONFLICTING" then "author"
                     elif ($other.at // "") > $last_commit_at then "author"
                     else "reviewer" end),
        ci: $ci,
        failing_checks: [$checks[] | select(.result == "FAILURE" or .result == "ERROR"
                                            or .result == "TIMED_OUT" or .result == "CANCELLED") | .name],
        size: {files: $pr.changedFiles, additions: $pr.additions, deletions: $pr.deletions},
        files: [$pr.files.nodes[].path],
        large_files: [$pr.files.nodes[] | select(.additions + .deletions >= 200)
                      | {path, lines: (.additions + .deletions)}],
        bulk_lines: ([$pr.files.nodes[] | select(.path | test(
                        "(^|/)(generated|vendor|third_party|snapshots?|__snapshots__|fixtures?|reference|data)/|\\.(lock|snap|jsonl|csv|tsv|parquet|svg|png|txt)$"))
                      | .additions + .deletions] | add // 0),
        template: {
          problem: ($pr.body | test("(^|\\n)## Problem")),
          solution: ($pr.body | test("(^|\\n)## Solution")),
          verification: ($pr.body | test("(^|\\n)## Verification")),
          unfilled: ($pr.body | test("<!--"))
        },
        closes: [$pr.closingIssuesReferences.nodes[] | {number, labels: [.labels.nodes[].name]}],
        mentions_stack: ($pr.body | test("(?i)stacked|stack of|base branch:")),
        stack: null
      }'
}

if [ "$stacks" -eq 0 ]; then
  emit
  exit 0
fi

# Native stack membership is only visible through the REST pull endpoint, so
# look it up per PR. A stack's bottom PR targets trunk, so every PR is checked.
emit | while IFS= read -r line; do
  number="$(jq -r .number <<<"$line")"
  stack="$(gh api "repos/$repo/pulls/$number" --jq '.stack' 2>/dev/null | jq -c . 2>/dev/null || true)"
  [ -z "$stack" ] && stack=null
  jq -c --argjson stack "$stack" '.stack = $stack' <<<"$line"
done
