---
name: write-docs
description: Decide what belongs in VibeSys documentation, write it in the house style, and add, move or delete pages without breaking links or the docs website. Use when adding, changing, moving or deleting a documentation page, a README, AGENTS.md, or a page's place in the docs website (its sidebar entry or its redirects).
metadata:
  kind: workflow
---

# Write docs

`docs/` is published twice: on GitHub, and as the website at
docs.vibing.systems, which Docusaurus builds from the website project with
`docs/` as its whole content root. A page id is the page's path under `docs/`
without the suffix; the website's sidebar configuration lists some of them,
and its Docusaurus configuration holds the redirects for moved pages.

## What may live in docs/

Only high-level, durable documentation: what the system is, where its
boundaries are, and why it is shaped that way. A page should change only when
the architecture changes. Everything else has another home in the placement
table of the `write-skills` skill
([Placement](../write-skills/SKILL.md#placement)); in short, recipes ("to add
X, edit Y") go in codebase skills, module descriptions in a README beside the
code, numbers and lists in the config or code that enforces them, and plans,
decision logs and research notes in issues and PRs.
Unenforced: placement needs judgment; reviewers apply the table.

Before writing a page, check that no page already covers the topic, and extend
it instead. Unenforced: telling two pages on one topic apart needs a reader.

## House style

- Write for a competent engineer with no context on this project.
- Define each project term where it first appears.
- Use plain words and complete sentences; give numbers in real units
  (seconds, GiB, requests per second), not "fast" or "a few".
- Cut anything that does not change what the reader decides or does next.
- Keep the fixed vocabulary: use the terms in the Vocabulary table of
  [sandboxing.md](../../../docs/contributing/sandboxing.md) (run environment,
  command runner, agent confinement, candidate isolation, trust boundary), and
  say "entrypoint", not "composition root".

Unenforced: style needs a reader, and "composition root" still appears in
current docs, so a banned-word check would start with false positives.

## Citing code

- When a docs page must cite code, cite it as `path:symbol`, never as a line
  range: a range keeps resolving after the code under it moves. A path suffix
  must name one tracked file. `check_doc_citations` enforces this for a gated
  set of pages (currently the
  [wire-protocol](../../../docs/contributing/wire-protocol.md) page). Known
  gap: other pages are unchecked; run `check_doc_citations` with the page as
  its argument before you finish. (This rule is about docs pages; skills
  themselves may not cite paths.)
- Link instead of copying. A table of CLI flags, messages or config keys
  copied from the parser or contract drifts from it; link the source, or
  generate the table from it. Unenforced: no check compares prose tables to
  their source.
- Links must resolve, including `#anchor`s, and a relative link inside
  `docs/` must stay inside `docs/` (link code through an absolute GitHub
  `blob/main` URL). `check_doc_links` enforces this.

## Adding a page

1. Place it by audience: user pages at the top level of `docs/`, contributor
   pages in its contributing section. Give it a title no other page uses.
   Unenforced: duplicate titles need a title-extraction check that does not
   exist yet; grep the H1s under `docs/` before choosing one.
2. Link it from the sidebar or from a related page. `check_doc_links` fails a
   page nothing links to, reading the sidebar; the pages that were orphans
   when that rule arrived sit in a shrink-only allowlist. Never add to it.

## Moving, renaming or deleting a page

1. Find every inbound reference: `git grep -n <basename>` across the whole
   repository, including code comments, tests, skills and `AGENTS.md`, not
   only Markdown. Update or remove each.
2. Update the page id in the website sidebar configuration.
3. For a page that was published, add a redirect from the old URL in the
   client-redirects plugin of the website's Docusaurus configuration, so
   published links do not turn into 404s.
4. If the page is in `check_doc_citations`'s gated set or `check_doc_links`'s
   orphan allowlist, update that entry.
5. Run the checks below.

Enforced: links from Markdown by `check_doc_links`; sidebar ids
and site-internal links by the website build, which fails on a broken link or
anchor. Unenforced: a reference in a code comment or a backticked path in
prose is invisible to both; step 1's grep is the only guard. Missing
redirects are also unchecked: nothing records which URLs were published.

## Known failure modes

- Prose names a symbol or path that a refactor removed. Enforced only for
  `path:symbol` citations in gated pages (`check_doc_citations`) and for paths
  into the examples tree
  (`test_example_paths_referenced_outside_examples_exist`).
  Unenforced elsewhere: a bare name in prose cannot be told from a word;
  grep the page for every renamed name in a refactor PR.
- A status line ("planned", "Phase N pending", "not yet built") outlives the
  work it describes. Unenforced: telling a stale plan from current fact needs
  a reader. Keep status in issues; if a page must say what is missing, say it
  as a current limit, not a schedule.
- Two pages with the same title, one of them stale. Unenforced: no check
  compares titles yet; see Adding a page, step 1.
- A copied flag, message or config table drifts from its parser or contract.
  Unenforced: see Citing code.
- A page linked from nowhere, served only to readers who know its URL.
  Enforced by `check_doc_links` (orphan check).

## Checks to run

- `check_doc_links`: links, anchors and orphans. CI runs it in an always-on
  docs job and in the website workflow.
- `check_doc_citations`: code citations in gated pages.
- For any change to `docs/` structure or the website: install dependencies
  and run the production build in the website project. The website workflow
  runs the build on pull requests that touch docs or the website, and the
  deploy workflow builds and deploys it on every push to main.
