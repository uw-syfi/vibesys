"""The boundary ratchets fail on a new or removed use, name the path, and pass a tree that matches."""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from scripts.check_boundaries import (
    DOC,
    env_read_violations,
    env_reads,
    env_uses,
    env_violations,
    is_product_code,
    mechanism_hits,
    mechanism_violations,
    proto_names,
    release_violations,
    unvalidated_hits,
    unvalidated_violations,
    words,
)

_NAMES = st.from_regex(r"(VIBESYS|XDG)_[A-Z]{1,8}(_[A-Z]{1,8}){0,2}", fullmatch=True)


def test_a_new_environment_name_fails_and_the_message_names_the_path_the_rule_and_the_fix() -> None:
    sources = {"orchestrator/hosted/new.go": 'var x = os.Getenv("VIBESYS_NEW_THING")\n'}

    (problem,) = env_violations(sources, {})

    assert problem.startswith("orchestrator/hosted/new.go: env-name: VIBESYS_NEW_THING")
    assert DOC in problem
    assert "typed message" in problem


def test_a_tree_that_matches_its_allowlist_is_clean() -> None:
    sources = {
        "cli/cmd/flags.go": 'a := "VIBESYS_RUN"\nb := "VIBESYS_RUN"\nc := "XDG_STATE_HOME"\n'
    }
    allowed = {"cli/cmd/flags.go": {"VIBESYS_RUN": 2, "XDG_STATE_HOME": 1}}

    assert env_violations(sources, allowed) == []


def test_a_removed_use_must_shrink_the_allowlist() -> None:
    allowed = {"cli/cmd/flags.go": {"VIBESYS_RUN": 1}}

    (problem,) = env_violations({"cli/cmd/flags.go": "package p\n"}, allowed)

    assert "cli/cmd/flags.go" in problem
    assert "shrink the entry" in problem


@given(name=_NAMES, count=st.integers(min_value=1, max_value=5))
def test_only_the_exact_count_passes(name: str, count: int) -> None:
    path = "gateway/internal/x.go"
    sources = {path: "".join(f'v{i} := "{name}"\n' for i in range(count))}

    assert env_violations(sources, {path: {name: count}}) == []
    assert env_violations(sources, {path: {name: count - 1}}) != []
    assert env_violations(sources, {path: {name: count + 1}}) != []


def test_every_spelling_of_a_read_is_counted() -> None:
    text = (
        'os.Getenv("VIBESYS_A")\n'
        "os.environ['VIBESYS_B']\n"
        "process.env.VIBESYS_C\n"
        'env=append(env, "VIBESYS_D="+v)\n'
        "`XDG_RUNTIME_DIR`\n"
        '"VIBESYS_" + suffix\n'
        "// VIBESYS_IN_A_COMMENT\n"
    )

    assert env_uses({"cli/x.go": text}) == {
        "cli/x.go": {
            "VIBESYS_A": 1,
            "VIBESYS_B": 1,
            "VIBESYS_C": 1,
            "VIBESYS_D": 1,
            "XDG_RUNTIME_DIR": 1,
        }
    }


def test_tests_docs_generated_code_and_the_python_path_are_not_scanned() -> None:
    quoted = 'x = "VIBESYS_A"\n'
    outside = [
        "cli/x_test.go",
        "cli/gen/vibesys/x.go",
        "orchestrator/internal/archcheck/a.go",
        "gateway/e2e/world.go",
        "clients/apps/desktop/src/a.test.ts",
        "clients/apps/desktop/playwright.config.ts",
        "clients/apps/desktop/playwright.fixture.config.ts",
        "sdk/vibesys-evaluator/python/tests/test_a.py",
        "docs/contributing/a.md",
        "src/vibesys/a.py",
        "install.sh",
    ]
    inside = [
        "cli/x.go",
        "orchestrator/sidecar/src/vs_sidecar/a.py",
        "clients/apps/desktop/src/main/a.ts",
    ]

    assert env_uses(dict.fromkeys(outside, quoted)) == {}
    assert all(is_product_code(path) for path in inside)
    assert len(env_uses(dict.fromkeys(inside, quoted))) == len(inside)


_PROTO = """
syntax = "proto3";
// socket_path in a comment is not a declaration
message Run {
  string run_id = 1;
  string socket_path = 2;
  oneof place {
    string sidecar_home = 3;
  }
  message Inner {
    string worktree = 1;
  }
  enum Kind {
    KIND_UNSPECIFIED = 0;
    KIND_DOCKER = 1;
  }
}
service Orchestrator {
  rpc StartRun(Run) returns (Run) {}
  rpc StopProcess(Run) returns (Run);
}
"""


def test_declared_names_are_qualified_by_their_message() -> None:
    assert set(proto_names(_PROTO)) >= {
        "Run",
        "Run.run_id",
        "Run.socket_path",
        "Run.place",
        "Run.sidecar_home",
        "Run.Inner",
        "Run.Inner.worktree",
        "Run.Kind",
        "Run.Kind.KIND_DOCKER",
        "Orchestrator.StartRun",
        "Orchestrator.StopProcess",
    }
    assert not any("comment" in name for name in proto_names(_PROTO))


def test_a_name_that_says_how_instead_of_what_fails_and_a_plain_one_does_not() -> None:
    path = "proto/vibesys/orchestrator/v1/x.proto"

    hits = mechanism_hits({path: _PROTO})

    assert hits[path] == [
        "Orchestrator.StopProcess",
        "Run.Inner.worktree",
        "Run.Kind.KIND_DOCKER",
        "Run.sidecar_home",
        "Run.socket_path",
    ]
    problems = mechanism_violations({path: _PROTO}, {})
    assert len(problems) == len(hits[path])
    assert all(
        problem.startswith(f"{path}: mechanism-word:") and DOC in problem for problem in problems
    )
    assert mechanism_violations({path: _PROTO}, hits) == []


def test_a_listed_name_that_is_gone_must_leave_the_list() -> None:
    path = "proto/vibesys/cli/v1/x.proto"

    (problem,) = mechanism_violations({path: "message A { string id = 1; }"}, {path: ["A.pid"]})

    assert path in problem
    assert "A.pid" in problem


def test_packages_of_the_python_path_are_not_held_to_the_word_list() -> None:
    assert mechanism_hits({"proto/vibesys/host/v1/x.proto": _PROTO}) == {}


@given(parts=st.lists(st.from_regex(r"[a-z]{2,6}", fullmatch=True), min_size=1, max_size=4))
def test_words_split_the_same_for_snake_camel_and_screaming_names(parts: list[str]) -> None:
    snake = "_".join(parts)
    camel = "".join(part.capitalize() for part in parts)
    screaming = snake.upper()

    assert words(snake) == parts
    assert words(screaming) == parts
    assert "".join(words(camel)) == "".join(parts)


_BREAKING = """
  # proto/vibesys/orchestrator/v1 is left out until the first tag.
  buf breaking --path proto/vibesys/cli/v1 \\
    --against base.tar
"""


def test_a_release_is_refused_while_the_orchestrator_contract_is_exempt() -> None:
    (problem,) = release_violations(_BREAKING)

    assert problem.startswith("scripts/check_proto_breaking.sh: release:")
    assert "--path proto/vibesys/orchestrator/v1" in problem


def test_a_release_passes_once_the_orchestrator_path_is_back_and_a_comment_does_not_count() -> None:
    restored = _BREAKING.replace("cli/v1", "cli/v1 --path proto/vibesys/orchestrator/v1")

    assert release_violations(restored) == []


def test_holding_one_orchestrator_file_does_not_count_as_holding_the_contract() -> None:
    one_file = _BREAKING.replace(
        "cli/v1", "cli/v1 --path proto/vibesys/orchestrator/v1/host_control.proto"
    )

    (problem,) = release_violations(one_file)

    assert problem.startswith("scripts/check_proto_breaking.sh: release:")


def test_the_breaking_check_in_the_repository_holds_the_orchestrator_contract() -> None:
    # The gate of a tag reads this file's text. scripts/tests/test_proto_breaking.sh runs buf to show
    # that holding the path fails a deleted or renamed field; this keeps the real text holding it.
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_proto_breaking.sh"

    assert release_violations(script.read_text(encoding="utf-8")) == []


_CLI = "proto/vibesys/cli/v1/x.proto"
_RULES = """
syntax = "proto3";
import "buf/validate/validate.proto";
message Ruled { string id = 1 [(buf.validate.field).string.min_len = 1]; }
message OneofRuled { oneof body { option (buf.validate.oneof).required = true; string a = 1; } }
message MessageRuled {
  option (buf.validate.message).cel = {id: "x" expression: "true"};
  string note = 1;
}
message Bare { string id = 1; /* buf.validate in a comment is not a rule */ }
message StopRequest {}
message Outer {
  string id = 1 [(buf.validate.field).string.min_len = 1];
  message Inner { string name = 1; }
}
"""


def test_a_message_with_fields_and_no_rule_counts_and_an_empty_or_ruled_one_does_not() -> None:
    assert unvalidated_hits({_CLI: _RULES}) == {_CLI: ["Bare", "Outer.Inner"]}


def test_a_nested_message_is_judged_on_its_own_rules_not_its_parents() -> None:
    parent_ruled = unvalidated_hits({_CLI: _RULES})[_CLI]

    assert "Outer" not in parent_ruled
    assert "Outer.Inner" in parent_ruled


def test_only_the_cli_package_is_held_to_the_rule_for_now() -> None:
    for package in ("orchestrator", "evaluator", "desktop", "host"):
        assert unvalidated_hits({f"proto/vibesys/{package}/v1/x.proto": _RULES}) == {}


def test_a_new_unvalidated_message_fails_and_a_newly_ruled_one_must_leave_the_list() -> None:
    (new,) = unvalidated_violations({_CLI: "message A { string id = 1; }"}, {})
    assert new.startswith(f"{_CLI}: unvalidated: A ")
    assert DOC in new

    (stale,) = unvalidated_violations(
        {_CLI: "message A { string id = 1 [(buf.validate.field).string.min_len = 1]; }"},
        {_CLI: ["A"]},
    )
    assert "remove it from" in stale

    assert unvalidated_violations({_CLI: "message A { string id = 1; }"}, {_CLI: ["A"]}) == []


def test_a_read_of_a_variable_by_name_fails_outside_the_codec_and_names_the_fix() -> None:
    sources = {"orchestrator/hosted/new.go": 'v := os.Getenv("VIBESYS_NEW_THING")\n'}

    (problem,) = env_read_violations(sources, {})

    assert problem.startswith("orchestrator/hosted/new.go: env-read: VIBESYS_NEW_THING")
    assert "cli/envcodec" in problem
    assert "env_name" in problem


@given(
    name=st.from_regex(r"VIBESYS_[A-Z]{1,8}(_[A-Z]{1,8}){0,2}", fullmatch=True),
    spelling=st.sampled_from(
        [
            'os.Getenv("{n}")',
            'os.LookupEnv("{n}")',
            'getenv("{n}")',
            'd.getenv("{n}")',
            "os.getenv('{n}')",
            "os.environ.get('{n}')",
            "os.environ['{n}']",
            "process.env.{n}",
            "process.env['{n}']",
        ]
    ),
)
def test_every_spelling_of_a_read_is_found_and_the_codec_is_exempt(
    name: str, spelling: str
) -> None:
    text = spelling.format(n=name) + "\n"

    assert env_reads({"cli/x.go": text}) == {"cli/x.go": {name: 1}}
    assert env_reads({"cli/envcodec/envcodec.go": text}) == {}


def test_a_name_that_is_only_written_is_not_a_read() -> None:
    text = 'usage := "set VIBESYS_RUN"\nenv = append(env, "VIBESYS_RUN="+v)\n'

    assert env_reads({"cli/x.go": text}) == {}


def test_a_listed_read_needs_its_exact_count_and_a_reason() -> None:
    sources = {"cli/x.go": 'os.Getenv("VIBESYS_A")\nos.Getenv("VIBESYS_A")\n'}
    listed = {"cli/x.go": {"VIBESYS_A": {"count": 2, "reason": "the installer's own setting"}}}

    assert env_read_violations(sources, listed) == []
    assert env_read_violations(sources, {"cli/x.go": {"VIBESYS_A": {"count": 1, "reason": "r"}}})
    (removed,) = env_read_violations({"cli/x.go": "package p\n"}, listed)
    assert "shrink the entry" in removed
    (unexplained,) = env_read_violations(sources, {"cli/x.go": {"VIBESYS_A": {"count": 2}}})
    assert "without a reason" in unexplained
