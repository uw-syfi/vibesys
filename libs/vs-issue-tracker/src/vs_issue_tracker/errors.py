"""Errors shared by issue tracker persistence adapters."""

from __future__ import annotations


class IssueTrackerLoadError(ValueError):
    """Stored tracker metadata is present but cannot be decoded."""

    @classmethod
    def issue_event_unclosed(cls) -> IssueTrackerLoadError:
        """Build an error for a truncated issue event record."""
        return cls("GitHub issue event metadata has no closing marker")

    @classmethod
    def invalid_issue_event(cls) -> IssueTrackerLoadError:
        """Build an error for an invalid issue event payload."""
        return cls("GitHub issue event metadata is invalid")

    @classmethod
    def progress_unclosed(cls) -> IssueTrackerLoadError:
        """Build an error for a truncated progress record."""
        return cls("GitHub progress metadata has no closing marker")

    @classmethod
    def invalid_progress(cls) -> IssueTrackerLoadError:
        """Build an error for an invalid progress payload."""
        return cls("GitHub progress metadata is invalid")

    @classmethod
    def non_text_progress(cls) -> IssueTrackerLoadError:
        """Build an error for a progress payload of the wrong type."""
        return cls("GitHub progress metadata is not a string")


__all__ = ["IssueTrackerLoadError"]
