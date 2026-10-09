"""Dependency-free kernel modules must remain visible in the architecture overview."""

from scripts.check_tach_graph import render_block


def test_declared_isolated_kernel_appears_without_inventing_dependencies() -> None:
    """Verify staleness detection and that isolated modules appear in the overview."""
    graph = render_block([("vs_runtime", "vs_project")], ("vs_runtime", "vs_project", "vs_core"))
    assert "[//]: # (tach-graph:start)" in graph
    assert "[//]: # (tach-graph:end)" in graph
    assert "## Architecture overview" in graph
    assert "    vs_core\n" in graph
    assert "vs_core -->" not in graph
    assert "--> vs_core" not in graph
