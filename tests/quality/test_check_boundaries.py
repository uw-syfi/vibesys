"""The boundary ratchets fail on a new or removed use, name the path, and pass a tree that matches."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from scripts.check_boundaries import (
    DOC,
    env_uses,
    env_violations,
    is_product_code,
    mechanism_hits,
    mechanism_violations,
    proto_names,
    release_violations,
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
