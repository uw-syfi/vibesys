# Omnigent Docker launcher spike

A runnable prototype for issue #239: can a VibeSys-owned Docker execution
environment be added *on top of* Omnigent, rather than accepting that Omnigent
ships no container provider? This spike answers yes for the launcher contract,
and marks exactly where the evidence stops.

It is an experiment artifact, not production wiring. It runs on Python 3.12
against `omnigent==0.6.0` (the current PyPI release) and is deliberately kept
out of `src/` because VibeSys CI runs Python 3.11, where Omnigent cannot
install. See [`docs/omnigent-evaluation.md`](../../docs/omnigent-evaluation.md)
for the full evaluation this supports.

## What it shows

`DockerSandboxLauncher` implements Omnigent's `SandboxLauncher` ABC and maps it
onto a VibeSys-owned container, injected exactly the way Omnigent's own module
docstring prescribes:

```python
ManagedSandboxConfig(
    server_url="http://host.docker.internal:6767",
    launcher_factory=lambda: DockerSandboxLauncher(sandbox, spec),
    token_ttl_s=90000,
)
```

The "isolate hard" boundary is the file layout:

| File | Imports `omnigent`? | Owns |
| --- | --- | --- |
| `spec.py` | no | VibeSys resource vocabulary (image, `--gpus`, `--device`, mounts) |
| `container.py` | no | Docker container lifecycle (create/exec/cp/stream/rm) |
| `omnigent_launcher.py` | **yes — the only one** | the `SandboxLauncher` ABC mapping |
| `smoke.py` | yes (test only) | the end-to-end proof |

If Omnigent's alpha ABC churns, only `omnigent_launcher.py` moves. If VibeSys
walks away, deleting that one file leaves the container code — which restates
what `libs/vs-sandbox` already owns — intact.

## Running it

```bash
uv venv --python 3.12 .venv-omni
. .venv-omni/bin/activate
uv pip install omnigent            # pulls omnigent-client + omnigent-ui-sdk, ~190 pkgs
docker pull python:3.12-bookworm   # or any image with a shell
python smoke.py
```

The smoke test needs a reachable Docker daemon. It creates a real container,
execs in it, and tears it down; it needs no GPU, no credentials, and no
Omnigent server.

## Result (recorded 2026-07-24, `omnigent==0.6.0`, Docker on Linux)

All ten checks pass:

```
[PASS] DockerSandboxLauncher is a concrete SandboxLauncher — unimplemented abstract methods: none
[PASS] ManagedSandboxConfig(launcher_factory=...) accepts the launcher — no registry patch, no fork
[PASS] GPU/device/shm resource args threaded into docker run argv
[PASS] provision() created a real container
[PASS] run() execs inside the container
[PASS] live host workspace visible inside container (bind mount)
[PASS] materialize_workspace resolves to bind mount without git clone
[PASS] agent writes propagate back to the host workspace
[PASS] is_running() reports the live container
[PASS] terminate() removes the container
```

The load-bearing findings: the container gap is a small adapter (three files,
one Omnigent import), the documented injection seam accepts it without a fork,
and the workspace-delivery override cleanly replaces Omnigent's default `git
clone` with a live bind mount — so the agent edits the real host workspace, as
the `AgentRunner` contract requires.

## What it deliberately does NOT prove

- **A full agent turn.** That needs the `omnigent` wheel baked into the agent
  image, a reachable server, real harness binaries, and provider credentials.
  Omnigent's model runs a whole `omnigent host` process inside the container
  that dials back to the server — heavier than VibeSys's one-subprocess-per-call
  path, and unverified here.
- **GPU passthrough executing on a GPU.** The `--gpus` / `--device` args are
  asserted on the `docker run` argv, not run on GPU hardware (none on this host).
- **Mid-run GPU reselect.** Omnigent's `provision(name: str) -> str` carries no
  per-session resource argument, so VibeSys's stop-container / change-device /
  restart / replay flow becomes terminate + re-provision. Not implemented here.
- **Gemini and OpenCode.** The released `0.6.0` `--harness` set is
  `claude-sdk, codex, cursor, kimi, openai-agents, open-responses, pi,
  antigravity, qwen, goose, copilot` — no `gemini`, no `opencode`, no `acp`.
  Two of VibeSys's four providers have no path on the installable release.
