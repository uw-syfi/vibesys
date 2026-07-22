from pathlib import Path

from vibesys.agents import cli_docker
from vibesys.agents.cli_docker import DockerAuthPath


def test_auth_import_copies_directories_and_files_to_private_writable_paths(
    tmp_path: Path, monkeypatch
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"synthetic": true}\n')
    monkeypatch.setitem(
        cli_docker.DOCKER_AUTH_PATHS,
        "fixture",
        [
            DockerAuthPath(state_dir, "/root/.fixture"),
            DockerAuthPath(auth_file, "/root/.fixture.json"),
        ],
    )

    assert cli_docker.auth_bind_mounts("fixture") == [
        (str(state_dir), "/opt/vibesys-auth/0", True),
        (str(auth_file), "/opt/vibesys-auth/1", True),
    ]
    assert cli_docker.auth_copy_commands("fixture") == [
        "mkdir -p /root/.fixture && cp -a /opt/vibesys-auth/0/. /root/.fixture/",
        "mkdir -p /root && cp -a /opt/vibesys-auth/1 /root/.fixture.json",
    ]


def test_auth_import_uses_provider_native_container_paths():
    assert {
        provider: [
            (spec.host_path.relative_to(Path.home()).as_posix(), spec.container_path)
            for spec in specs
        ]
        for provider, specs in cli_docker.DOCKER_AUTH_PATHS.items()
    } == {
        "claude": [
            (".claude/.credentials.json", "/root/.claude/.credentials.json"),
            (".claude/settings.json", "/root/.claude/settings.json"),
            (".claude/settings.local.json", "/root/.claude/settings.local.json"),
            (".claude.json", "/root/.claude.json"),
        ],
        "gemini": [
            (".gemini/oauth_creds.json", "/root/.gemini/oauth_creds.json"),
            (".gemini/google_accounts.json", "/root/.gemini/google_accounts.json"),
            (".gemini/settings.json", "/root/.gemini/settings.json"),
            (".gemini/.env", "/root/.gemini/.env"),
        ],
        "codex": [
            (".codex/auth.json", "/root/.codex/auth.json"),
            (".codex/config.toml", "/root/.codex/config.toml"),
        ],
        "opencode": [
            (
                ".local/share/opencode/auth.json",
                "/root/.local/share/opencode/auth.json",
            ),
            (".config/opencode/opencode.json", "/root/.config/opencode/opencode.json"),
            (
                ".config/opencode/opencode.jsonc",
                "/root/.config/opencode/opencode.jsonc",
            ),
            (".config/opencode/config.json", "/root/.config/opencode/config.json"),
            (
                ".config/opencode/config.jsonc",
                "/root/.config/opencode/config.jsonc",
            ),
            (".config/opencode/.env", "/root/.config/opencode/.env"),
        ],
    }


def test_provider_auth_imports_exclude_bulk_runtime_roots():
    configured_sources = {
        spec.host_path for specs in cli_docker.DOCKER_AUTH_PATHS.values() for spec in specs
    }

    assert configured_sources.isdisjoint(
        {
            Path.home() / ".claude",
            Path.home() / ".gemini",
            Path.home() / ".codex",
            Path.home() / ".local" / "share" / "opencode",
            Path.home() / ".config" / "opencode",
        }
    )
