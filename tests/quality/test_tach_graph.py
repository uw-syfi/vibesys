"""Dependency-free kernel modules must remain visible in architecture graphs."""

from scripts.check_tach_graph import render_block


def test_declared_isolated_kernel_appears_without_inventing_dependencies() -> None:
    graph = render_block([("vs_runtime", "vs_project")], ("vs_runtime", "vs_project", "vs_core"))
    assert graph.count("    vs_core\n") == 2
    assert "vs_core -->" not in graph
    assert "--> vs_core" not in graph
