"""Strategy additions require no framework or shared-message edits."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from scripts.check_proto_layers import main, violations


@given(owner=st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True))
def test_strategy_package_can_use_own_messages_framework_and_shared(owner: str) -> None:
    path = f"proto/vibesys/strategies/{owner}/v1/request.proto"
    source = f"""syntax = "proto3"; package vibesys.strategies.{owner}.v1;
import "vibesys/store/v1/span.proto";
import "vibesys/core/v1/common.proto";
import "vibesys/strategies/{owner}/v1/reply.proto";
"""
    assert violations({path: source}) == ()
    bad = source + 'import "vibesys/strategies/other_owner/v1/reply.proto";'
    if owner != "other_owner":
        assert path in violations({path: bad})[0]


@pytest.mark.parametrize("layer", ["core", "store"])
@pytest.mark.parametrize("modifier", ["", "public ", "weak "])
def test_lower_message_layers_reject_strategy_and_legacy_policy_imports(
    layer: str, modifier: str
) -> None:
    path = f"proto/vibesys/{layer}/v1/shared.proto"
    assert violations({path: f'import {modifier}"vibesys/strategies/search/v1/reply.proto";'})
    assert violations({path: 'import "vibesys/search/v1/attrs.proto";'})
    assert not violations({path: 'import "vibesys/core/v1/common.proto";'})


def test_strategy_package_must_match_owner_directory() -> None:
    path = "proto/vibesys/strategies/dynamic/v1/request.proto"
    assert path in violations({path: "package vibesys.strategies.single.v1;"})[0]


@pytest.mark.parametrize(
    "path",
    [
        "libs/vs-core/src/vs_core/flows/flow.py",
        "libs/vs-runlog/src/vs_runlog/view.py",
        "src/headless/view.py",
        "src/vibesys/records/view.py",
        "libs/vs-proto/src/vs_proto/api.py",
    ],
)
@pytest.mark.parametrize(
    "source",
    [
        "from vs_proto.api.strategies.dynamic import Request",
        "import vs_proto.api.strategies.dynamic as strategy",
        "from vs_proto.api import strategies",
    ],
)
def test_generic_python_consumers_cannot_import_strategy_messages(path: str, source: str) -> None:
    assert path in violations({path: source})[0]
    assert violations({path: "from vs_proto.api import core_common"}) == ()


def test_viewer_uses_shared_facts_and_generic_descriptor_views() -> None:
    path = "clients/libs/run-state/src/view.ts"
    assert violations(
        {path: 'import {Request} from "@vibesys/proto/strategies/dynamic/request_pb.js";'}
    )
    assert not violations({path: 'import {Span} from "@vibesys/proto/store/v1/span_pb.js";'})
    generated = "clients/libs/proto/src/generated/vibesys/strategies/dynamic/request_pb.ts"
    assert not violations({generated: 'import {Reply} from "../strategies/dynamic/reply.js";'})


def test_repository_message_layers_respect_ownership() -> None:
    assert main() == 0
