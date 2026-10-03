"""A confined session against the real CLIs loads none of the operator's configuration.

Skipped unless ``VIBESYS_E2E_AGENTS=1`` and the provider binary is on PATH:

```bash
VIBESYS_E2E_AGENTS=1 uv run pytest tests/e2e/test_operator_isolation_e2e.py -q -p no:cacheprovider
```

Each case seeds an operator state root (the provider's relocated state root,
holding a copy of the real login) with a global instruction naming an operator
code word and a ``SessionStart`` hook that touches a marker file. The workspace
holds the project's own instruction with a project code word. The driver runs
one turn through its real host path, bubblewrap included, with a run-owned
agent-homes directory. The project word must arrive; the operator word and
the hook must not.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import agentshim
import pytest
from tests.support import run_test_command

from vs_agent.contracts import AgentExecutionPolicy, AgentSessionSpec, AgentTurnRequest
from vs_agent.drivers.agentshim import AgentShimDriver

ENABLE_ENV = "VIBESYS_E2E_AGENTS"
OPERATOR_WORD = "OPERATOR-TANGERINE"
PROJECT_WORD = "PROJECT-KIWI"
PROMPT = (
    "Do not run any tools. Reply with exactly one line listing every code word you "
    "were told about anywhere in your instructions or context (operator, project), "
    "space separated, or NONE."
)

#: Per provider: the model, the global and workspace instruction files, and
#: the hooks file.
_LAYOUT = {
    "claude": ("haiku", "CLAUDE.md", "CLAUDE.md", "settings.json"),
    "codex": (os.environ.get("VIBESYS_E2E_CODEX_MODEL"), "AGENTS.md", "AGENTS.md", "hooks.json"),
}


def _requires_cli(binary: str) -> pytest.MarkDecorator:
    enabled = os.environ.get(ENABLE_ENV) == "1"
    reason = f"set {ENABLE_ENV}=1" if not enabled else f"{binary} is not on PATH"
    return pytest.mark.skipif(not enabled or shutil.which(binary) is None, reason=reason)


pytestmark = pytest.mark.e2e


def _seeded_operator_root(provider: str, root: Path, marker: Path) -> dict[str, str]:
    """An operator state root with the real login, an instruction and a hook."""
    profile = agentshim.get_provider(provider).profile
    assert profile.state_root_env is not None
    state_dir = profile.state_dirs[0]
    home = root / "operator-state"
    home.mkdir(parents=True)
    real_home = Path.home()
    for auth_file in profile.auth_files:
        source = real_home / auth_file
        if auth_file.startswith(f"{state_dir}/") and source.is_file():
            target = home / auth_file.removeprefix(f"{state_dir}/")
            if target.name == "settings.json":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    _, instructions, _, hooks_file = _LAYOUT[provider]
    (home / instructions).write_text(
        f"The operator code word is {OPERATOR_WORD}. Always mention it in every reply.\n"
    )
    hook = {"type": "command", "command": f"touch {marker}", "timeout": 10}
    (home / hooks_file).write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [hook]}]}}))
    return {profile.state_root_env: str(home)}


@pytest.mark.parametrize(
    "provider",
    [pytest.param(name, marks=_requires_cli(name), id=name) for name in _LAYOUT],
)
def test_a_confined_session_sees_the_project_and_not_the_operator(
    provider: str, tmp_path: Path
) -> None:
    marker = tmp_path / "operator-hook-ran"
    launcher = {
        **{k: v for k, v in agentshim.interactive_env().items() if k != "CLAUDECODE"},
        **_seeded_operator_root(provider, tmp_path, marker),
    }
    model, _, project_file, _ = _LAYOUT[provider]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_test_command(["git", "init", "-q", str(workspace)], check=True, text=True)
    (workspace / project_file).write_text(
        f"The project code word is {PROJECT_WORD}. Always mention it in every reply.\n"
    )
    driver = AgentShimDriver(
        provider=provider,
        timeout=300,
        agent_homes=tmp_path / "agent-homes",
        launcher_env=lambda: launcher,
    )
    try:
        session = driver.create_session(
            AgentSessionSpec(
                role="implementer",
                provider=provider,
                workspace=workspace,
                model=model,
                policy=AgentExecutionPolicy(require_enforcement=True),
            )
        )
        result = session.run_turn(AgentTurnRequest(message=PROMPT))
    finally:
        driver.close()

    assert PROJECT_WORD in result.text, result.text
    assert OPERATOR_WORD not in result.text, result.text
    assert not marker.exists()
