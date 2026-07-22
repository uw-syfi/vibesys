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
        provider: [spec.container_path for spec in specs]
        for provider, specs in cli_docker.DOCKER_AUTH_PATHS.items()
    } == {
        "claude": ["/root/.claude", "/root/.claude.json"],
        "gemini": ["/root/.gemini"],
        "codex": ["/root/.codex"],
        "opencode": [
            "/root/.local/share/opencode",
            "/root/.config/opencode",
        ],
    }
