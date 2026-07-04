"""Tests for the per-issue markdown renderer (vibe_serve/plain/render.py)."""

from vibe_serve.loops.plain.render import (
    issue_md_filename,
    issue_md_path,
    render_all,
    render_index_markdown,
    render_issue_file,
    render_issue_markdown,
    slugify,
)
from vs_issue_board import (
    Issue,
    IssueBoard,
    IssueEvent,
    IssueStatus,
    IssueType,
)


def _make_issue(
    *,
    id: int = 1,
    title: str = "Test issue",
    description: str = "A test issue.",
    type: IssueType = IssueType.FEATURE,
    status: IssueStatus = IssueStatus.OPEN,
    attempts: int = 0,
    created_by: str = "loop:bootstrap",
    history: list[IssueEvent] | None = None,
) -> Issue:
    now = "2026-04-08T12:00:00"
    return Issue(
        id=id,
        type=type,
        title=title,
        description=description,
        status=status,
        created_by=created_by,
        created_iter=1,
        created_at=now,
        updated_at=now,
        attempts=attempts,
        history=history
        or [
            IssueEvent(
                timestamp=now,
                actor=created_by,
                action="create",
                iteration=1,
            )
        ],
    )


def _make_event(
    *,
    actor: str,
    action: str,
    iteration: int = 1,
    note: str = "",
    payload: dict | None = None,
    timestamp: str = "2026-04-08T12:01:00",
) -> IssueEvent:
    return IssueEvent(
        timestamp=timestamp,
        actor=actor,
        action=action,
        iteration=iteration,
        note=note,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic_lowercases_and_dashes(self):
        assert slugify("Add Paged KV cache") == "add-paged-kv-cache"

    def test_empty_title_falls_back_to_untitled(self):
        assert slugify("") == "untitled"

    def test_punctuation_only_falls_back_to_untitled(self):
        assert slugify("!!!") == "untitled"
        assert slugify("---") == "untitled"

    def test_whitespace_only_falls_back_to_untitled(self):
        assert slugify("   ") == "untitled"

    def test_collapses_multiple_separators(self):
        assert slugify("foo  BAR___baz") == "foo-bar-baz"

    def test_strips_leading_and_trailing_dashes(self):
        assert slugify("--foo-bar--") == "foo-bar"

    def test_handles_unicode_via_normalisation(self):
        # em-dash and accented chars should not blow up
        assert slugify("foo  BAR—baz") == "foo-bar-baz"
        assert slugify("café au lait") == "cafe-au-lait"

    def test_truncation_to_max_len(self):
        long_title = "a" * 200
        slug = slugify(long_title)
        assert len(slug) <= 40

    def test_truncation_strips_trailing_dash(self):
        # 39 letters then a separator, truncated to 40 → "...x-" → strip → "...x"
        title = "abcdefghij" * 4 + "-end"  # 44 chars; truncates inside the dash
        slug = slugify(title)
        assert not slug.endswith("-")

    def test_custom_max_len(self):
        assert slugify("abcdefghij", max_len=5) == "abcde"


# ---------------------------------------------------------------------------
# filename / path
# ---------------------------------------------------------------------------


class TestIssueMdFilename:
    def test_filename_format_uses_zero_padded_id_and_slug(self):
        issue = _make_issue(id=7, title="Add streaming completions")
        assert issue_md_filename(issue) == "0007-add-streaming-completions.md"

    def test_filename_for_high_id(self):
        issue = _make_issue(id=12345, title="x")
        assert issue_md_filename(issue) == "12345-x.md"

    def test_path_lives_under_issues_dir(self, tmp_path):
        issue = _make_issue(id=3, title="hello world")
        path = issue_md_path(tmp_path / "issues", issue)
        assert path == tmp_path / "issues" / "0003-hello-world.md"


# ---------------------------------------------------------------------------
# render_issue_markdown
# ---------------------------------------------------------------------------


class TestRenderIssueMarkdown:
    def test_writes_header_with_id_title_type_status(self):
        issue = _make_issue(
            id=42,
            title="Build server",
            type=IssueType.FEATURE,
            status=IssueStatus.OPEN,
        )
        md = render_issue_markdown(issue)
        assert "# #0042 — Build server" in md
        assert "**Type**: feature" in md
        assert "**Status**: open" in md
        assert "**Attempts**: 0" in md

    def test_includes_description(self):
        issue = _make_issue(description="Run a FastAPI server on port 8000.")
        md = render_issue_markdown(issue)
        assert "## Description" in md
        assert "Run a FastAPI server on port 8000." in md

    def test_includes_timeline_with_events(self):
        issue = _make_issue(
            history=[
                _make_event(
                    actor="loop:bootstrap",
                    action="create",
                    timestamp="2026-04-08T12:00:00",
                ),
                _make_event(
                    actor="loop",
                    action="open->in_progress",
                    note="claimed",
                    timestamp="2026-04-08T12:01:00",
                ),
            ],
        )
        md = render_issue_markdown(issue)
        assert "## Timeline" in md
        assert "loop:bootstrap" in md
        assert "create" in md
        assert "open->in_progress" in md
        assert "claimed" in md

    def test_renders_implementer_payload_section(self):
        issue = _make_issue(
            attempts=1,
            history=[
                _make_event(actor="loop:bootstrap", action="create"),
                _make_event(
                    actor="implementer",
                    action="attempt",
                    note="built it",
                    payload={
                        "issue_id": 1,
                        "summary": "built x",
                        "files_touched": ["server.py", "tests/test_x.py"],
                        "self_check": "all green",
                    },
                ),
            ],
        )
        md = render_issue_markdown(issue)
        assert "## Attempt detail" in md
        assert "### Implementer attempt 1" in md
        assert "built x" in md
        assert "`server.py`" in md
        assert "`tests/test_x.py`" in md
        assert "all green" in md

    def test_renders_judge_payload_section(self):
        issue = _make_issue(
            history=[
                _make_event(actor="loop:bootstrap", action="create"),
                _make_event(actor="implementer", action="attempt"),
                _make_event(
                    actor="judge",
                    action="in_progress->open",
                    note="not done",
                    payload={
                        "issue_id": 1,
                        "verdict": "fail",
                        "analysis": "the endpoint is missing",
                        "feedback": "add /v1/completions",
                        "new_issues_filed": [],
                    },
                ),
            ],
        )
        md = render_issue_markdown(issue)
        assert "### Judge review 1" in md
        assert "**Verdict**: FAIL" in md
        assert "the endpoint is missing" in md
        assert "add /v1/completions" in md

    def test_judge_event_disambiguated_from_loop_status_change(self):
        """A loop-actor open->in_progress (claim) must NOT be rendered as a judge review."""
        issue = _make_issue(
            history=[
                _make_event(actor="loop:bootstrap", action="create"),
                # loop's claim — same arrow format but actor='loop'
                _make_event(
                    actor="loop",
                    action="open->in_progress",
                    note="claimed",
                ),
            ],
        )
        md = render_issue_markdown(issue)
        # The claim must show in Timeline, but no "Judge review" detail section.
        assert "## Timeline" in md
        assert "Judge review" not in md
        # And no Attempt detail section if there are no payloads
        assert "## Attempt detail" not in md

    def test_handles_events_without_payload(self):
        """Events with payload=None must render plain timeline rows without raising."""
        issue = _make_issue(
            history=[
                _make_event(actor="loop:bootstrap", action="create"),
                _make_event(actor="implementer", action="attempt", payload=None),
            ],
        )
        md = render_issue_markdown(issue)
        # No detail section for the payload-less attempt
        assert "## Attempt detail" not in md
        # Timeline still mentions it
        assert "attempt" in md

    def test_idempotent_no_duplicate_sections(self, tmp_path):
        issue = _make_issue(
            attempts=1,
            history=[
                _make_event(actor="loop:bootstrap", action="create"),
                _make_event(
                    actor="implementer",
                    action="attempt",
                    payload={"summary": "x", "files_touched": [], "self_check": "ok"},
                ),
            ],
        )
        issues_dir = tmp_path / "issues"
        path1 = render_issue_file(issues_dir, issue)
        first = path1.read_text(encoding="utf-8")
        path2 = render_issue_file(issues_dir, issue)
        second = path2.read_text(encoding="utf-8")
        assert first == second
        # Only one occurrence of the heading
        assert second.count("### Implementer attempt 1") == 1

    def test_closed_iter_appears_when_set(self):
        issue = _make_issue(status=IssueStatus.CLOSED)
        issue.closed_iter = 3
        md = render_issue_markdown(issue)
        assert "**Closed at iter**: 3" in md

    def test_long_description_is_not_escaped(self):
        """Markdown special chars in the description pass through unmodified
        (matches the existing progress.md behaviour at vibe_serve/plain/loop.py:147)."""
        issue = _make_issue(description="# A markdown heading\n\n- a list")
        md = render_issue_markdown(issue)
        assert "# A markdown heading" in md
        assert "- a list" in md


# ---------------------------------------------------------------------------
# render_index_markdown
# ---------------------------------------------------------------------------


class TestRenderIndexMarkdown:
    def test_empty_store_renders_placeholder(self):
        md = render_index_markdown([])
        assert "# Issue Index" in md
        assert "no issues yet" in md

    def test_groups_issues_by_status_in_display_order(self):
        issues = [
            _make_issue(id=1, title="open one", status=IssueStatus.OPEN),
            _make_issue(id=2, title="closed one", status=IssueStatus.CLOSED),
            _make_issue(id=3, title="in progress one", status=IssueStatus.IN_PROGRESS),
            _make_issue(id=4, title="blocked one", status=IssueStatus.BLOCKED),
        ]
        md = render_index_markdown(issues)
        # Status headings appear in order: in_progress, open, blocked, closed
        ip = md.find("## in_progress")
        op = md.find("## open")
        bl = md.find("## blocked")
        cl = md.find("## closed")
        assert -1 < ip < op < bl < cl

    def test_index_links_to_per_issue_files(self):
        issues = [_make_issue(id=7, title="Add streaming")]
        md = render_index_markdown(issues)
        assert "[Add streaming](0007-add-streaming.md)" in md

    def test_within_status_sorted_by_id(self):
        issues = [
            _make_issue(id=3, title="three", status=IssueStatus.OPEN),
            _make_issue(id=1, title="one", status=IssueStatus.OPEN),
            _make_issue(id=2, title="two", status=IssueStatus.OPEN),
        ]
        md = render_index_markdown(issues)
        # Find the position of each link in the output
        p1 = md.find("[one]")
        p2 = md.find("[two]")
        p3 = md.find("[three]")
        assert -1 < p1 < p2 < p3

    def test_table_columns_present(self):
        issues = [_make_issue(id=1, title="x", attempts=2)]
        md = render_index_markdown(issues)
        assert "| ID | Type | Title | Attempts | Created iter | Updated |" in md
        assert "| 1 |" in md

    def test_pipe_in_title_is_escaped(self):
        issues = [_make_issue(id=1, title="foo | bar")]
        md = render_index_markdown(issues)
        # Escaped pipe so the markdown table cell still parses as one cell
        assert "foo \\| bar" in md


# ---------------------------------------------------------------------------
# render_all integration
# ---------------------------------------------------------------------------


class TestRenderAll:
    def test_render_all_writes_index_and_per_issue_files(self, tmp_path):
        store = IssueBoard(tmp_path / "issues.json")
        a = store.create(
            type=IssueType.FEATURE,
            title="Build server",
            description="d",
            created_by="loop:bootstrap",
            iteration=1,
        )
        store.create(
            type=IssueType.BUG,
            title="Crash on startup",
            description="d",
            created_by="judge",
            iteration=1,
        )
        store.update_status(a.id, IssueStatus.IN_PROGRESS, actor="loop", iteration=1)
        store.increment_attempts(
            a.id,
            actor="implementer",
            iteration=1,
            payload={"summary": "did stuff", "files_touched": ["s.py"], "self_check": "ok"},
        )

        issues_dir = tmp_path / "issues"
        render_all(issues_dir, store)

        assert (issues_dir / "INDEX.md").is_file()
        assert (issues_dir / "0001-build-server.md").is_file()
        assert (issues_dir / "0002-crash-on-startup.md").is_file()

        index = (issues_dir / "INDEX.md").read_text()
        assert "Build server" in index
        assert "Crash on startup" in index

        per_a = (issues_dir / "0001-build-server.md").read_text()
        assert "did stuff" in per_a
        assert "`s.py`" in per_a

    def test_render_all_creates_issues_dir(self, tmp_path):
        store = IssueBoard(tmp_path / "issues.json")
        store.create(
            type=IssueType.BUG,
            title="t",
            description="d",
            created_by="judge",
            iteration=1,
        )
        issues_dir = tmp_path / "nested" / "issues"
        assert not issues_dir.exists()
        render_all(issues_dir, store)
        assert issues_dir.is_dir()
        assert (issues_dir / "INDEX.md").is_file()

    def test_render_all_idempotent(self, tmp_path):
        store = IssueBoard(tmp_path / "issues.json")
        store.create(
            type=IssueType.BUG,
            title="repeat",
            description="d",
            created_by="judge",
            iteration=1,
        )
        issues_dir = tmp_path / "issues"
        render_all(issues_dir, store)
        first_files = sorted(p.read_bytes() for p in issues_dir.glob("*.md"))
        render_all(issues_dir, store)
        second_files = sorted(p.read_bytes() for p in issues_dir.glob("*.md"))
        assert first_files == second_files
