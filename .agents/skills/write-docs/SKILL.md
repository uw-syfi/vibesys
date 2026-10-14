---
name: write-docs
description: Decide what belongs in VibeSys documentation, write it in the house style, and add, move or delete pages without breaking links or the docs website. Use when adding, changing, moving or deleting a page in docs/, a README, AGENTS.md, or the docs website in website/ (sidebars.ts, docusaurus.config.ts redirects).
metadata:
  kind: workflow
---

# Write docs

`docs/` is published twice: on GitHub, and as the website at
docs.vibing.systems, which Docusaurus builds from `website/` with `docs/` as
its whole content root. Page ids are paths under `docs/` without the suffix;
`website/sidebars.ts` lists some of them, and `website/docusaurus.config.ts`
holds the redirects for moved pages.

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

- Cite code as `path:symbol` (for example
  `scripts/check_doc_citations.py:GATED_DOCS`), never as a line range: a range
  keeps resolving after the code under it moves. A path suffix must name one
  tracked file. Enforced by `scripts/check_doc_citations.py` for the pages in
  its `GATED_DOCS`, which today is only
  [wire-protocol.md](../../../docs/contributing/wire-protocol.md). Known gap:
  other pages are unchecked; survey one with
  `python3 -m scripts.check_doc_citations docs/<page>.md` before you finish.
- Link instead of copying. A table of CLI flags, messages or config keys
  copied from the parser or contract drifts from it; link the source, or
  generate the table from it. Unenforced: no check compares prose tables to
  their source.
- Links must resolve, including `#anchor`s, and a relative link inside
  `docs/` must stay inside `docs/` (use an absolute
  `https://github.com/uw-syfi/vibesys/blob/main/...` URL for code). Enforced by
  `scripts/check_doc_links.py`.

## Adding a page

1. Place it by audience: user pages at the top of `docs/`, contributor pages
   under `docs/contributing/`. Give it a title no other page uses.
   Unenforced: duplicate titles need a title-extraction check that does not
   exist yet; grep the H1s under `docs/` before choosing one.
2. Link it from the sidebar or from a related page. A page nothing links to
   fails `scripts/check_doc_links.py`, which reads the sidebar; the pages that
   were orphans when that rule arrived are recorded in
   `scripts/check_doc_links.py:ORPHAN_ALLOWLIST`. Never add to it.

## Moving, renaming or deleting a page

1. Find every inbound reference: `git grep -n <basename>` across the whole
   repository, including code comments, tests, skills and `AGENTS.md`, not
   only Markdown. Update or remove each.
2. Update the page id in `website/sidebars.ts`.
3. For a page that was published, add a redirect from the old URL in the
   client-redirects plugin of `website/docusaurus.config.ts`, so published
   links do not turn into 404s.
4. If the page is in `scripts/check_doc_citations.py:GATED_DOCS` or
   `scripts/check_doc_links.py:ORPHAN_ALLOWLIST`, update that entry.
5. Run the checks below.

Enforced: links from Markdown by `scripts/check_doc_links.py`; sidebar ids
and site-internal links by the website build, which fails on a broken link or
anchor. Unenforced: a reference in a code comment or a backticked path in
prose is invisible to both; step 1's grep is the only guard. Missing
redirects are also unchecked: nothing records which URLs were published.

## Known failure modes

- Prose names a symbol or path that a refactor removed. Enforced only for
  `path:symbol` citations in gated pages (`scripts/check_doc_citations.py`)
  and for paths under examples/
  (`tests/examples/test_example_registry.py:test_example_paths_referenced_outside_examples_exist`).
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
  Enforced by `scripts/check_doc_links.py` (orphan check).

## Checks to run

- `python3 scripts/check_doc_links.py`: links, anchors and orphans. CI runs
  it in the always-on `doc-checks` job of `.github/workflows/test.yml` and in
  `.github/workflows/website.yml`.
- `python3 -m scripts.check_doc_citations`: code citations in gated pages.
- For any change to `docs/` structure or `website/`: `npm ci` and
  `npm run build` in `website/`. `.github/workflows/website.yml` runs the
  build on pull requests that touch `docs/` or `website/`, and
  `.github/workflows/deploy-docs.yml` builds and deploys it on every push to
  main.
