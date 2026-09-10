# Agent Instructions

Read [`docs/contributing/coding-best-practices.md`](docs/contributing/coding-best-practices.md) before
editing code in this repository. It documents the repo-specific expectations
for what good code looks like.

When preparing a pull request, use the repository PR template at
[`.github/pull_request_template.md`](.github/pull_request_template.md). Fill in
the `Problem`, `Solution`, and `Verification` sections, including correctness
properties and testing details where relevant.

Keep changes narrowly scoped to the requested behavior, preserve existing
architecture boundaries, and run the smallest relevant checks before handing
work back.

Before touching the agent CLI integration, read "Where agentshim lives" in
[`docs/contributing/agent-drivers.md`](docs/contributing/agent-drivers.md): it
says which repository owns provider knowledge and which owns driver policy.

# Delegation

The main agent does orchestration: planning high-level effort, monitoring
progress, and steering. Delegate detailed work (searching, reading, and
editing across files) to subagents. Use cheaper models whenever the task
allows, and run independent subagents in parallel.

Before creating a GitHub issue, read
[`docs/contributing/issue-authoring.md`](docs/contributing/issue-authoring.md) and the matching form under
`.github/ISSUE_TEMPLATE/`. Search both the codebase and open and closed issues
before filing. Use the repo-local `create-issue` skill when it is available.

For `resources/skills/serving-systems/`, also follow the subtree-specific
authoring guide in
[`resources/skills/serving-systems/CLAUDE.md`](resources/skills/serving-systems/CLAUDE.md).

# Writing style

Applies to both chat replies and docs.

- Be concise. Prefer the shortest version that is still precise. Cut preamble,
  recaps, and restating the question.
- Do not over-explain or hand-hold. Assume the reader knows generic CS.
- Use precise technical CS terms; keep them. Drop inflated or
  self-congratulatory jargon and piled-up metaphors (e.g. "blast radius", "earn
  their keep", "the frontier is the feature"). Say the plain precise thing
  instead.
- Direct engineering voice: lead with the answer, then support it.
- Tables and short code are fine when they carry information; keep them small.
  Put exhaustive detail in an appendix, not at the top of a doc.
- No em dashes. Use commas, colons, parentheses, or separate sentences instead.
