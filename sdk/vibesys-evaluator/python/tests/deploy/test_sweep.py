"""The sweep deletes exactly this task's stale namespaces, never another's, never a new UID."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from vibesys_evaluator.deploy._k8s import CREATED_LABEL, TASK_LABEL, sweep

from .k8s_world import make_app, make_world

TTL = 3600 + 600  # the task's longest timeout_seconds plus the margin


@st.composite
def namespaces(draw: st.DrawFn) -> list[tuple[str, dict[str, str], float]]:
    """Namespaces of this task, of another task, and unlabelled, of every age around the TTL."""
    found = []
    for index in range(draw(st.integers(0, 8))):
        task = draw(st.sampled_from(["shop", "shop-2", "other", None]))
        prefix = draw(st.sampled_from(["hk8s-shop-", "hk8s-shop-2-", "hk8s-other-", "kube-"]))
        age = draw(st.sampled_from([0.0, TTL - 1, TTL + 1, 10 * TTL]))
        labels = {} if task is None else {TASK_LABEL: task}
        if draw(st.booleans()):
            labels[CREATED_LABEL] = str(int(-age))  # relative to the clock's start, fixed below
        found.append((f"{prefix}{index:04x}", labels, age))
    return found


@given(namespaces())
def test_only_this_tasks_old_namespaces_are_deleted(
    listed: list[tuple[str, dict[str, str], float]],
) -> None:
    with tempfile.TemporaryDirectory() as root:
        world = make_world(Path(root))
        now = world.clock.now()
        expected = set()
        for name, labels, age in listed:
            if CREATED_LABEL in labels:
                labels[CREATED_LABEL] = str(int(now - age))
            world.kube.add_namespace(name, labels, created=now - age)
            if (
                labels.get(TASK_LABEL) == "shop"
                and name.startswith("hk8s-shop-")
                and "-" not in name.removeprefix("hk8s-shop-")
                and age > TTL
            ):
                expected.add(name)
        swept = sweep(world.source(make_app()), cluster=world.cluster)
        assert set(swept) == expected
        survivors = set(world.kube.live_namespaces())
        assert survivors == {name for name, _, _ in listed} - expected


def test_a_namespace_recreated_between_listing_and_deletion_is_kept(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    old = world.clock.now() - 10 * TTL
    world.kube.add_namespace(
        "hk8s-shop-aaaa", {TASK_LABEL: "shop", CREATED_LABEL: str(int(old))}, created=old
    )
    # Call 1 lists; call 2 is the deletion: another job recreates the namespace just before.
    world.kube.at_call(2, lambda: world.kube.recreate("hk8s-shop-aaaa"))
    swept = sweep(world.source(make_app()), cluster=world.cluster)
    assert swept == []
    assert world.kube.live_namespaces() == ["hk8s-shop-aaaa"]
    assert any("Conflict" in line for line in world.lines)
