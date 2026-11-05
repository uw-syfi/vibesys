"""deploy.k8s.App: every field is checked at construction, and errors name the field."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from k8s_world import make_app


def test_a_valid_app_freezes_its_collections() -> None:
    app = make_app()
    assert app.restartable == ("frontend", "backend")
    with pytest.raises(TypeError):
        app.images["OTHER"] = "x"  # type: ignore[index]


@pytest.mark.parametrize(
    ("overrides", "names"),
    [
        ({"name": "Shop"}, "name="),
        ({"manifest": "/abs/manifest.yaml"}, "manifest="),
        ({"manifest": "../manifest.yaml"}, "manifest="),
        ({"images": {"app": "app"}}, "images"),
        ({"images": {"NAMESPACE": "app"}}, "images"),
        ({"images": {"APP_IMAGE": "../app"}}, "images['APP_IMAGE']"),
        ({"gateway": "https://frontend:5000"}, "gateway="),
        ({"gateway": "http://frontend:5000/api"}, "gateway="),
        ({"gateway": "http://Front_End:5000"}, "gateway="),
        ({"ready": []}, "ready="),
        ({"data_tier": "tier in (data)"}, "data_tier="),
        ({"seed": None}, "data_tier="),
        ({"restartable": ["frontend", "frontend"]}, "restartable"),
        ({"caches": {"cache": "varnish"}}, "caches['cache']"),
    ],
)
def test_a_wrong_field_is_named(overrides: dict[str, object], names: str) -> None:
    with pytest.raises(ValueError, match=names.replace("[", r"\[").replace("]", r"\]")):
        make_app(**overrides)


@given(st.text(min_size=1, max_size=50))
def test_names_are_dns_safe_or_refused(name: str) -> None:
    valid = (
        len(name) <= 40
        and name[0].isalpha()
        and name[0].islower()
        and not name.endswith("-")
        and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name)
    )
    if not valid:
        with pytest.raises(ValueError, match="name="):
            make_app(name=name)
        return
    assert make_app(name=name).name == name
