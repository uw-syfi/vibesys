"""Manifest rendering: substitution, the kinds allowlist, the namespace, the data-tier split."""

from __future__ import annotations

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from k8s_world import MANIFEST
from vibesys_evaluator.deploy._k8s import ALLOWED_KINDS, ManifestError, render

IMAGES = {"APP_IMAGE": "registry:5000/vse/shop/app:0123456789abcdef"}


def test_variables_are_substituted_and_every_object_gets_the_namespace() -> None:
    rendered = render(MANIFEST, namespace="hk8s-shop-1", images=IMAGES, data_tier={})
    assert "${" not in rendered.text
    assert all(obj["metadata"]["namespace"] == "hk8s-shop-1" for obj in rendered.objects)
    frontend = next(
        o
        for o in rendered.objects
        if o["kind"] == "Deployment" and o["metadata"]["name"] == "frontend"
    )
    assert frontend["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGES["APP_IMAGE"]
    config = next(o for o in rendered.objects if o["kind"] == "ConfigMap")
    assert config["data"]["db"] == "db.hk8s-shop-1.svc.cluster.local:27017"


def test_an_unknown_variable_is_named_with_its_line() -> None:
    with pytest.raises(ManifestError, match=r"manifest:6: unknown variable \$\{DB_IMAGE\}"):
        render(
            MANIFEST.replace("db.${NAMESPACE}", "${DB_IMAGE}"),
            namespace="n",
            images=IMAGES,
            data_tier={},
        )


def test_an_image_variable_the_manifest_does_not_use_is_refused() -> None:
    with pytest.raises(ManifestError, match="images\\['OTHER_IMAGE'\\] is not used"):
        render(MANIFEST, namespace="n", images={**IMAGES, "OTHER_IMAGE": "x"}, data_tier={})


def test_an_object_of_another_namespace_is_refused() -> None:
    text = MANIFEST.replace('namespace: "${NAMESPACE}"', "namespace: default")
    with pytest.raises(ManifestError, match=r"Service frontend: metadata\.namespace"):
        render(text, namespace="n", images=IMAGES, data_tier={})


KINDS = st.sampled_from(
    [
        ("v1", "Namespace"),
        ("rbac.authorization.k8s.io/v1", "ClusterRole"),
        ("rbac.authorization.k8s.io/v1", "Role"),
        ("apps/v1", "DaemonSet"),
        ("v1", "PersistentVolume"),
        ("v1", "Pod"),
        ("batch/v1", "Job"),
        ("apiextensions.k8s.io/v1", "CustomResourceDefinition"),
        ("v1", "Service"),
        ("apps/v1", "Deployment"),
        ("v1", "ConfigMap"),
        ("policy/v1", "PodDisruptionBudget"),
        ("example.com/v1", "Deployment"),
    ]
)


@given(KINDS)
def test_only_allowlisted_kinds_render(kind: tuple[str, str]) -> None:
    api_version, name = kind
    obj = {"apiVersion": api_version, "kind": name, "metadata": {"name": "x"}}
    if name == "Deployment":
        obj["spec"] = {"selector": {"matchLabels": {"a": "b"}}}
    text = yaml.safe_dump(obj)
    allowed = (api_version.rpartition("/")[0], name) in ALLOWED_KINDS
    if not allowed:
        with pytest.raises(ManifestError, match="kind not allowed"):
            render(text, namespace="n", images={}, data_tier={})
        return
    assert len(render(text, namespace="n", images={}, data_tier={}).objects) == 1


LABELS = st.dictionaries(
    st.sampled_from(["tier", "app"]), st.sampled_from(["data", "app", "web"]), max_size=2
)


@given(
    st.lists(
        st.tuples(st.sampled_from(["Deployment", "StatefulSet", "Service", "ConfigMap"]), LABELS),
        min_size=1,
        max_size=8,
    )
)
def test_the_data_tier_split_keeps_every_object_once_and_orders_by_selector(
    objects: list[tuple[str, dict[str, str]]],
) -> None:
    documents = []
    for index, (kind, labels) in enumerate(objects):
        api = "apps/v1" if kind in ("Deployment", "StatefulSet") else "v1"
        documents.append(
            {"apiVersion": api, "kind": kind, "metadata": {"name": f"o{index}", "labels": labels}}
        )
    text = yaml.safe_dump_all(documents)
    selector = {"tier": "data"}
    workloads_in_tier = [
        labels.get("tier") == "data"
        for kind, labels in objects
        if kind in ("Deployment", "StatefulSet")
    ]
    if not any(workloads_in_tier):
        with pytest.raises(ManifestError, match="selects no Deployment"):
            render(text, namespace="n", images={}, data_tier=selector)
        return
    rendered = render(text, namespace="n", images={}, data_tier=selector)
    names = [o["metadata"]["name"] for o in rendered.objects]
    assert sorted(names) == sorted(f"o{i}" for i in range(len(objects)))
    for obj in rendered.rest:
        assert obj["kind"] in ("Deployment", "StatefulSet")
        assert obj["metadata"]["labels"].get("tier") != "data"
    for obj in rendered.first:
        assert (
            obj["kind"] not in ("Deployment", "StatefulSet")
            or obj["metadata"]["labels"]["tier"] == "data"
        )


def test_without_a_data_tier_everything_is_applied_at_once() -> None:
    rendered = render(MANIFEST, namespace="n", images=IMAGES, data_tier={})
    assert rendered.rest == ()
    assert len(rendered.first) == 7
