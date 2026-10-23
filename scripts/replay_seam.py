"""Replay the seam properties on a historical commit, to see which bugs they find there.

For one target commit the script makes a detached worktree and gives it the seams the tests need:
the session factory (the I/O seam commit, restricted to the connection package), the gateway
preload factory with its channel capabilities (rewritten in place when the target predates
#3434), and the transport in libs/ipc. It then overlays this checkout's seam tests and test
support, runs them, and removes the worktree. None of these adapters changes how a target
handles a reply or an error; they only name the places the tests inject into. Where a target
lacks the page's `gatewayBridge` or `loadView`, the adapter appends the identity bridge and
a projection of what that code shows (`error`), so the same assertions apply.
A non-zero exit means a property failed on that commit. Logs go to ``.agent-logs/``.

This is local tooling, not a CI test: it needs git, pnpm, bun (the version pinned in
``packaging/fetch_bun.py``) and uv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SEAM_SUBJECT = "^refactor(clients): gateway I/O seams"

# Tests that state the properties, and the test support they need, copied from this checkout.
CLIENT_TESTS = {
    "gateway": "clients/apps/desktop-main/src/preload/seam-dst.test.ts",
    "hosts": "clients/features/hosts/src/main/host-version-sequences.test.ts",
    "older-host": "clients/apps/desktop-main/src/preload/host-seam.test.ts",
    "accounting": "clients/features/run-page/src/state/accounting-seam.test.ts",
}
CLIENT_SUPPORT = (
    "clients/apps/desktop-main/src/testing/gateway-world.ts",
    "clients/libs/connection/src/testing/schedule.ts",
    "clients/libs/connection/src/testing/gateway-session.ts",
    "clients/libs/ipc/src/testing/gateway-port.ts",
    "clients/libs/store/src/testing/seeded.ts",
    "proto/testdata/grpc_status/statuses.json",
)
# Export lines appended to the target's testing entry points, which differ per commit.
TESTING_EXPORTS = {
    "clients/libs/connection/src/testing/index.ts": (
        "export {FakeGatewaySession, type GatewayWireCall} from './gateway-session.js';",
        "export {HeldReply, Schedule} from './schedule.js';",
    ),
    "clients/libs/store/src/testing/index.ts": ("export {seedsAtDepth} from './seeded.js';",),
    "clients/libs/ipc/src/testing/index.ts": (
        "export {FakeGatewayPort} from './gateway-port.js';",
    ),
}
HISTORY_FILES = (
    "tests/vibesys/run/test_seam_history.py",
    "tests/vibesys/run/fixtures/seam-history",
)


def run(
    argv: list[str], cwd: Path, *, env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run bounded tooling with literal argument vectors, retaining all output."""
    # lint-waiver: LW-343202 [S603]; Resolved executables receive literal argv without a shell. A product subprocess adapter would add runtime dependencies to this offline checkout tool; reimplementing Git, uv or Bun would duplicate the tools.
    return subprocess.run(  # noqa: S603
        argv,
        cwd=cwd,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


def executable(name: str) -> str:
    """Resolve a required tool before any checkout mutation."""
    result = shutil.which(name)
    if result is None:
        message = f"{name} is required"
        raise RuntimeError(message)
    return result


def checked(argv: list[str], cwd: Path, *, stdin: str | None = None) -> str:
    """Fail setup independently of any expected property failure."""
    result = run(argv, cwd, stdin=stdin)
    if result.returncode:
        message = f"setup failed: {argv[0]} {argv[1:]}\n{result.stdout}\n{result.stderr}"
        raise RuntimeError(message)
    return result.stdout


def copy(relative: str, checkout: Path) -> None:
    """Copy one file or directory from this checkout into the historical one."""
    source, target = ROOT / relative, checkout / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)


def link_workspace(clients: Path) -> None:
    """Link every workspace package into the packages the overlaid tests import from.

    The overlay can import a package the target's manifest did not yet depend on; a link is all
    Bun needs to resolve it, and the manifests of the target stay untouched.
    """
    names = {
        json.loads(manifest.read_text())["name"]: manifest.parent
        for manifest in [
            *clients.glob("libs/*/package.json"),
            *clients.glob("features/*/package.json"),
        ]
    }
    for consumer in ("apps/desktop-main", "features/hosts", "features/run-page", "libs/ipc"):
        scope = clients / consumer / "node_modules/@vibesys"
        scope.mkdir(parents=True, exist_ok=True)
        for name, directory in names.items():
            link = scope / name.removeprefix("@vibesys/")
            if not link.exists():
                link.symlink_to(directory, target_is_directory=True)


SEAM_FILES = (
    "clients/libs/connection/src/shell/gateway.ts",
    "clients/libs/connection/src/shell/grpc.ts",
    "clients/libs/connection/src/index.ts",
)
PRELOAD = "clients/apps/desktop-main/src/preload/welcome.cts"
IPC = "clients/libs/ipc/src"
STATE_INDEX = "clients/features/run-page/src/state/index.ts"
LOAD_VIEW = """
export type LoadView = {
  readonly kind: 'ready' | 'loading' | 'reconnecting' | 'failed';
  readonly word: string;
};
// Replay adapter: what a target without `loadView` shows, from the fields it has.
export function loadView(context: {
  readonly error: string | null;
  readonly load?: {readonly complete?: boolean};
}): LoadView {
  if (context.error === 'Not on this host') return {kind: 'failed', word: 'No run log'};
  if (context.error !== null) return {kind: 'failed', word: context.error};
  return context.load?.complete === true
    ? {kind: 'ready', word: ''}
    : {kind: 'loading', word: ''};
}
"""


def adapt_preload(text: str) -> str:
    """Rewrite a pre-#3434 preload so its gateway bridge is built from channel capabilities."""
    if "function createGatewayPreload(" in text:
        return text
    begin = text.index("  electron.contextBridge.exposeInMainWorld('vibesysGateway', {")
    end = text.index("  prepareFirstPaint", begin)
    block = text[begin:end]
    close = re.search(r"  } satisfies import\('@vibesys/ipc'\)\.\w+\);\n", block)
    if close is None:
        message = "unsupported preload shape: the gateway bridge is not a satisfies literal"
        raise ValueError(message)
    literal = block[block.index("{") : close.start() + 3]
    literal = literal.replace(
        "openGateway(\n          'unary',",
        "openGateway(\n          capabilities,\n          'unary',",
    )
    literal = literal.replace("openGateway('stream',", "openGateway(capabilities, 'stream',")
    wiring = (
        "  electron.contextBridge.exposeInMainWorld(\n    'vibesysGateway',\n"
        "    createGatewayPreload({\n"
        "      newChannel: () => new MessageChannel(),\n"
        "      open: (request, port) => electron.ipcRenderer.postMessage('vibesys:gateway', request, [port]),\n"
        "    }),\n  );\n"
    )
    text = text[:begin] + wiring + text[end:]
    text = text.replace(
        "export = createWelcomePreload;",
        "function createGatewayPreload(capabilities: any): any {\n  return "
        + literal
        + ";\n}\n\ncreateWelcomePreload.gateway = createGatewayPreload;\nexport = createWelcomePreload;",
    )
    text, count = re.subn(
        r"function openGateway\(\n", "function openGateway(\n  capabilities: any,\n", text
    )
    text = text.replace(
        "  const electron: typeof import('electron') = require('electron');\n  const {port1, port2} = new MessageChannel();",
        "  const {port1, port2} = capabilities.newChannel();",
    )
    text = text.replace(
        "  electron.ipcRenderer.postMessage('vibesys:gateway', {kind, host, method, bytes}, [port2]);",
        "  capabilities.open({kind, host, method, bytes}, port2);",
    )
    if count != 1 or "new MessageChannel();\n  let closed" in text:
        message = "unsupported preload shape: openGateway differs"
        raise ValueError(message)
    return text


def overlay_client(checkout: Path, suites: list[str]) -> None:
    """Install the seams, the tests and their support on the target's client tree."""
    git = executable("git")
    seam = checked(
        [git, "log", "-1", "--format=%H", f"--grep={SEAM_SUBJECT}", "HEAD"], ROOT
    ).strip()
    patch = checked([git, "diff", f"{seam}^", seam, "--", *SEAM_FILES], ROOT)
    checked([git, "apply", "--3way"], checkout, stdin=patch)
    preload = checkout / PRELOAD
    preload.write_text(adapt_preload(preload.read_text()))
    transport = checkout / IPC / "gateway-transport.ts"
    shell_transport = checkout / "clients/apps/desktop-shell/src/gateway-transport.ts"
    if shell_transport.exists() and not transport.exists():
        transport.write_text(
            shell_transport.read_text().replace("from '@vibesys/ipc'", "from './gateway.js'")
        )
    ipc_index = checkout / IPC / "index.ts"
    index_text = ipc_index.read_text()
    lines = []
    if "gatewayTransport" not in index_text:
        lines.append("export {gatewayTransport} from './gateway-transport.js';")
    if "gatewayBridge" not in index_text:
        lines.append("export const gatewayBridge = <T>(preload: T): T => preload;")
    ipc_index.write_text(index_text + "\n" + "\n".join(lines) + "\n")
    state = checkout / STATE_INDEX
    if "loadView" not in state.read_text():
        state.write_text(state.read_text() + LOAD_VIEW)
    for relative in (*CLIENT_SUPPORT, *(CLIENT_TESTS[name] for name in suites)):
        copy(relative, checkout)
    for relative, lines in TESTING_EXPORTS.items():
        with (checkout / relative).open("a") as entry:
            entry.write("\n" + "\n".join(lines) + "\n")
    checked(
        [executable("pnpm"), "install", "--offline", "--no-frozen-lockfile"], checkout / "clients"
    )
    link_workspace(checkout / "clients")


def replay(args: argparse.Namespace) -> int:
    """Run the selected suites on the target commit and print one outcome line per suite."""
    git = executable("git")
    bun = str(Path(args.bun).resolve()) if args.bun else executable("bun")
    commit = checked([git, "rev-parse", "--verify", f"{args.ref}^{{commit}}"], ROOT).strip()
    logs = ROOT / ".agent-logs"
    logs.mkdir(exist_ok=True)
    status = 0
    with TemporaryDirectory(prefix="vibesys-seam-replay-") as temporary:
        checkout = Path(temporary) / "checkout"
        checked([git, "worktree", "add", "--detach", str(checkout), commit], ROOT)
        try:
            client = [name for name in args.suite if name in CLIENT_TESTS]
            if client:
                overlay_client(checkout, client)
                files = [CLIENT_TESTS[name].removeprefix("clients/") for name in client]
                result = run(
                    [bun, "test", "--conditions=@vibesys/source", *files],
                    checkout / "clients",
                    env={**os.environ, "SEAM_REPLAY": "1"},
                )
                status |= report(logs, commit, "client", result)
            if "history" in args.suite:
                for relative in HISTORY_FILES:
                    copy(relative, checkout)
                result = run(
                    [
                        executable("uv"),
                        "run",
                        "--locked",
                        "pytest",
                        HISTORY_FILES[0],
                        "-n",
                        "0",
                        "--no-cov",
                        "--runxfail",
                        "-q",
                    ],
                    checkout,
                )
                status |= report(logs, commit, "history", result)
        finally:
            checked([git, "worktree", "remove", "--force", str(checkout)], ROOT)
    return status


def report(
    logs: Path,
    commit: str,
    suite: str,
    result: subprocess.CompletedProcess[str],
) -> int:
    """Keep the full output and print the failing test names."""
    log = logs / f"replay-{commit[:9]}-{suite}.log"
    log.write_text(result.stdout + result.stderr)
    failed = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if line.startswith(("(fail)", "FAILED", "ERROR"))
    ]
    print(f"{commit[:9]} {suite}: exit {result.returncode}; {log.relative_to(ROOT)}")
    # Rows share their property text after "NAME: "; count the rows that fail each property.
    properties: dict[str, int] = {}
    for line in dict.fromkeys(failed):
        name = re.sub(r" \[[\d.]+ms\]$", "", line.removeprefix("(fail) "))
        key = name.split(": ", 1)[1] if line.startswith("(fail)") and ": " in name else name
        properties[key] = properties.get(key, 0) + 1
    for key, count in properties.items():
        print(f"  {count} x {key}")
    return result.returncode


def main() -> int:
    """Parse the explicit replay target and the suites to run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument(
        "--suite",
        nargs="+",
        choices=(*CLIENT_TESTS, "history"),
        default=[*CLIENT_TESTS, "history"],
    )
    parser.add_argument("--bun", help="the Bun pinned in packaging/fetch_bun.py")
    return replay(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
