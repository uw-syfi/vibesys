from __future__ import annotations

import ast
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from dotenv import dotenv_values
from hypothesis import given
from hypothesis import strategies as st

from entrypoints.web_home.keys import store_key

if TYPE_CHECKING:
    from tests.entrypoints.web_home.support import Home

SAMPLE_KEY = "sk-test-0123456789"


def _provider(home: Home, name: str) -> dict[str, Any]:
    providers = home.get("/api/auth").json()["providers"]
    return next(p for p in providers if p["provider"] == name)


def test_status_reports_sources_without_values(home: Home) -> None:
    home.config.dotenv_path.parent.mkdir(parents=True)
    home.config.dotenv_path.write_text(f"ANTHROPIC_API_KEY={SAMPLE_KEY}\n")
    home.config.environ = {**home.config.environ, "OPENAI_API_KEY": "sk-env"}

    reply = home.get("/api/auth")
    claude = _provider(home, "claude")
    codex = _provider(home, "codex")

    assert SAMPLE_KEY.encode() not in reply.body
    assert b"sk-env" not in reply.body
    assert claude["status"] == "key"
    assert {"name": "ANTHROPIC_API_KEY", "source": "dotenv", "shadowed": False} in claude["keys"]
    assert codex["keys"] == [{"name": "OPENAI_API_KEY", "source": "env", "shadowed": False}]
    assert _provider(home, "opencode")["keys"] == []


def test_a_cli_session_honors_the_state_root_variable(home: Home, tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    home.config.environ = {**home.config.environ, "CODEX_HOME": str(codex_home)}

    codex = _provider(home, "codex")

    assert (codex["status"], codex["cli_session"], codex["login_command"]) == (
        "cli_session",
        "present",
        "codex login",
    )


@pytest.mark.parametrize(
    ("found", "status", "session"),
    [(True, "cli_session", "present"), (False, "missing", "absent"), (None, "missing", "unknown")],
)
def test_claude_sign_in_is_read_from_the_keychain_by_presence_only(
    home: Home, *, found: bool | None, status: str, session: str
) -> None:
    asked: list[str] = []

    def keychain(service: str) -> bool | None:
        asked.append(service)
        return found

    home.config.keychain = keychain

    claude = _provider(home, "claude")

    assert (claude["status"], claude["cli_session"]) == (status, session)
    assert asked == ["Claude Code-credentials"]


def test_a_credentials_file_wins_without_asking_the_keychain(home: Home) -> None:
    credentials = Path(home.config.environ["HOME"]) / ".claude" / ".credentials.json"
    credentials.parent.mkdir()
    credentials.write_text("{}")
    asked: list[str] = []
    home.config.keychain = lambda service: asked.append(service) or None

    assert _provider(home, "claude")["cli_session"] == "present"
    assert asked == []


def test_writing_a_key_is_write_only_private_and_preserves_other_entries(home: Home) -> None:
    path = home.config.dotenv_path
    path.parent.mkdir(parents=True)
    path.write_text("# keep me\nOTHER=1\nOPENAI_API_KEY=old\n")

    reply = home.put("/api/auth/codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY})

    assert reply.status == 200
    assert SAMPLE_KEY.encode() not in reply.body
    assert reply.json() == {
        "provider": "codex",
        "name": "OPENAI_API_KEY",
        "status": "unverified",
        "shadowed_by_env": False,
    }
    assert path.read_text() == f"# keep me\nOTHER=1\nOPENAI_API_KEY='{SAMPLE_KEY}'\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _provider(home, "codex")["status"] == "key"


def test_writing_a_key_strips_surrounding_whitespace(home: Home) -> None:
    path = home.config.dotenv_path
    path.parent.mkdir(parents=True)

    reply = home.put("/api/auth/codex", {"name": "OPENAI_API_KEY", "value": f"  {SAMPLE_KEY}\t"})

    assert reply.status == 200
    assert path.read_text() == f"OPENAI_API_KEY='{SAMPLE_KEY}'\n"


@pytest.mark.parametrize("inherited", ["sk-stale", ""])
def test_an_inherited_variable_shadows_the_saved_key_even_when_empty(
    home: Home, inherited: str
) -> None:
    home.config.environ = {**home.config.environ, "OPENAI_API_KEY": inherited}

    reply = home.put("/api/auth/codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY}).json()
    [key] = _provider(home, "codex")["keys"]

    assert reply["shadowed_by_env"] is True
    assert key == {
        "name": "OPENAI_API_KEY",
        "source": "env" if inherited else "missing",
        "shadowed": True,
    }


@pytest.mark.parametrize(
    ("provider", "body", "code"),
    [
        ("nope", {"name": "X_API_KEY", "value": SAMPLE_KEY}, "unknown_provider"),
        ("codex", {"name": "OPENAI_BASE_URL", "value": SAMPLE_KEY}, "invalid_key"),
        ("codex", {"name": "PATH", "value": SAMPLE_KEY}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": "   "}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": "sk\nEVIL=1"}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": "sk'x"}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": "sk${HOME}"}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": 'sk"x'}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": "sk" + chr(0x2028) + "x"}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": "sk" + chr(0xE9)}, "invalid_key"),
        ("codex", {"name": SAMPLE_KEY, "value": SAMPLE_KEY}, "invalid_key"),
        ("codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY, "extra": 1}, "invalid_request"),
        ("codex", {"name": "OPENAI_API_KEY", "value": 12345}, "invalid_request"),
    ],
)
def test_rejected_keys_never_echo_the_value_or_touch_the_file(
    home: Home, provider: str, body: dict[str, object], code: str
) -> None:
    reply = home.put(f"/api/auth/{provider}", body)

    assert reply.json()["error"]["code"] == code
    assert SAMPLE_KEY.encode() not in reply.body
    assert b"12345" not in reply.body
    assert not home.config.dotenv_path.exists()


def test_a_non_utf8_env_file_is_a_typed_error_and_untouched(home: Home) -> None:
    path = home.config.dotenv_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"A=\xff\n")

    status = home.get("/api/auth")
    reply = home.put("/api/auth/codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY})

    assert (status.status, status.json()["error"]["code"]) == (400, "invalid_request")
    assert (reply.status, reply.json()["error"]["code"]) == (400, "invalid_request")
    assert path.read_bytes() == b"A=\xff\n"


def test_a_symlinked_env_file_is_refused(home: Home, tmp_path: Path) -> None:
    target = tmp_path / "real.env"
    target.write_text("A=1\n")
    home.config.dotenv_path.parent.mkdir(parents=True)
    home.config.dotenv_path.symlink_to(target)

    reply = home.put("/api/auth/codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY})

    assert reply.json()["error"]["code"] == "symlink_rejected"
    assert target.read_text() == "A=1\n"


def test_a_multiline_value_that_mentions_the_key_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = 'CERT="line one\nOPENAI_API_KEY=not-a-binding\n"\nOPENAI_API_KEY=old\n'
    path.write_text(original)

    store_key(path, "OPENAI_API_KEY", SAMPLE_KEY)

    assert (
        path.read_text()
        == 'CERT="line one\nOPENAI_API_KEY=not-a-binding\n"\n' + f"OPENAI_API_KEY='{SAMPLE_KEY}'\n"
    )


def test_crlf_files_keep_their_line_endings(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"# windows\r\nOTHER=1\r\nOPENAI_API_KEY=old\r\n")

    store_key(path, "OPENAI_API_KEY", SAMPLE_KEY)

    assert (
        path.read_bytes()
        == b"# windows\r\nOTHER=1\r\n" + f"OPENAI_API_KEY='{SAMPLE_KEY}'\n".encode()
    )


def test_writing_a_key_leaves_this_process_environment_alone(home: Home) -> None:
    before = dict(os.environ)
    home.put("/api/auth/codex", {"name": "OPENAI_API_KEY", "value": SAMPLE_KEY})

    assert dict(os.environ) == before


def test_the_home_server_never_imports_the_dotenv_loaders() -> None:
    # Catches both `from dotenv import load_dotenv` and `import dotenv; dotenv.load_dotenv(...)`.
    forbidden = {"load_dotenv", "load_config", "find_dotenv"}
    package = Path(__file__).parents[3] / "src" / "entrypoints" / "web_home"
    names: set[str] = set()
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)

    assert names.isdisjoint(forbidden)


_NAMES = st.from_regex(r"[A-Z][A-Z0-9_]{0,8}", fullmatch=True).filter(
    lambda n: n != "OPENAI_API_KEY"
)
_VALUES = st.text(
    # Zs (Unicode space separators, e.g. NBSP) is excluded too: python-dotenv's own
    # unquoted-value parser rstrips it, so a trailing one would not round-trip through
    # dotenv_values even untouched by store_key, unrelated to what this test checks.
    st.characters(
        codec="utf-8", exclude_categories=("Cc", "Cs", "Zs"), exclude_characters="'\\\"$#\n"
    ),
    min_size=1,
    max_size=20,
)
_KEY = st.text(
    st.characters(codec="utf-8", exclude_categories=("Cc", "Cs"), exclude_characters="'\"\\$"),
    min_size=1,
    max_size=40,
).filter(str.strip)


@given(entries=st.dictionaries(_NAMES, _VALUES, max_size=5), key=_KEY)
def test_storing_a_key_round_trips_and_keeps_every_other_entry(
    tmp_path_factory: pytest.TempPathFactory, entries: dict[str, str], key: str
) -> None:
    path = tmp_path_factory.mktemp("env") / ".env"
    path.write_text("".join(f"{name}={value}\n" for name, value in entries.items()))

    store_key(path, "OPENAI_API_KEY", key)

    assert dotenv_values(path) == {**entries, "OPENAI_API_KEY": key}
