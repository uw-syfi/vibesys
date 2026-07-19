"""Tests for the shared plain-text formatting helpers."""

from vibesys.render import format_status_prefix, format_token_count
from vibesys.server.events import AgentStatusData


class TestFormatTokenCount:
    def test_zero(self):
        assert format_token_count(0) == "0"

    def test_under_thousand(self):
        assert format_token_count(523) == "523"
        assert format_token_count(999) == "999"

    def test_thousands_boundary(self):
        assert format_token_count(1000) == "1k"
        assert format_token_count(20_100) == "20k"
        assert format_token_count(199_500) == "199k"
        assert format_token_count(999_999) == "999k"

    def test_millions(self):
        assert format_token_count(1_000_000) == "1.0M"
        assert format_token_count(1_200_000) == "1.2M"
        assert format_token_count(1_048_576) == "1.0M"


class TestFormatStatusPrefix:
    def test_none_status_is_empty(self):
        assert format_status_prefix(None) == ""

    def test_anonymous_status_is_empty(self):
        status = AgentStatusData(elapsed_seconds=3.2, input_tokens=100)
        assert format_status_prefix(status) == ""

    def test_full_prefix(self):
        status = AgentStatusData(
            progress="Round 3/24",
            agent_label="Implementer",
            elapsed_seconds=12.34,
            input_tokens=20_100,
            context_window=1_000_000,
        )
        assert format_status_prefix(status) == "[Round 3/24 | Implementer | 12.3s | 20k/1.0M] "

    def test_no_context_window_omits_max(self):
        status = AgentStatusData(agent_label="X", elapsed_seconds=0.0, input_tokens=0)
        assert format_status_prefix(status) == "[X | 0.0s | 0] "

    def test_progress_only(self):
        status = AgentStatusData(progress="Round 1/2", elapsed_seconds=1.0, input_tokens=500)
        assert format_status_prefix(status) == "[Round 1/2 | 1.0s | 500] "
