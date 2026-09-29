# Electron Shell Implementation Plan (Sub-project 6)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One command opens VibeSys as a desktop app: an Electron main process starts `vibesys web home` from the checkout, waits for it, and shows the React app in a native macOS window; quitting stops that home server.

**Architecture:** A new workspace package `clients/desktop` holds three small modules: `policy.ts` (pure origin, request, permission and argv rules), `home.ts` (the home server child process: spawn in its own process group, read the capability line from stdout, health check, stop, crash classification) and `index.ts` (Electron wiring: single-instance lock, window, menu, navigation and request guards, quit). A four-line sandboxed preload exposes only the platform so `clients/web` can inset the traffic lights. Dev runs `electron-vite dev`, which serves `clients/web` through its own Vite config with hot reload; prod loads the built `clients/web/dist` from the home server. No Python change: the shell uses the `--port` and `--dev-origin` flags plan 2 already ships.

**Tech Stack:** Electron 44.4.5 (exact; `pnpm view electron version` on 2026-09-28), electron-vite 5.0.0 (exact; peer `vite ^7`), TypeScript 5.8 strict, Node stdlib (`child_process`, `readline`, `fetch`), bun test with the `node:test` API, Playwright 1.55.0 `_electron` (already the workspace's pin), Biome 2.5.

**Spec:** `docs/superpowers/specs/2026-09-28-web-app-design.md`, section "6. Electron shell" (read the whole spec). Home server contract: `docs/superpowers/plans/2026-09-28-2-home-server.md` ("API contract", "Transport") and the landed `src/entrypoints/web_home/app.py`. App: `docs/superpowers/plans/2026-09-28-3-app-shell.md` (in the `web-app-shell` worktree).

## Global Constraints

- Scope: sub-project 6 only. New: `clients/desktop/**`. Modified: `clients/package.json`, `clients/pnpm-workspace.yaml`, `clients/pnpm-lock.yaml`, `clients/knip.jsonc`, `clients/.dependency-cruiser.cjs`, `clients/scripts/check_ts_package_manifests.mjs`, `clients/scripts/check_ts_architecture.test.mjs`, `tests/repoctl/cases.toml`, `.gitignore`, `clients/web/src/main.tsx`, `clients/web/src/window.css`, `docs/contributing/web-development.md`. No Python file changes.
- Packaging, signing, notarization, installers and auto-update are out of scope (spec: "Distribution ... is a later spec"); the app runs from a checkout.
- Exact pins: `electron@44.4.5`, `electron-vite@5.0.0`. Playwright stays at the root's `@playwright/test@1.55.0`, unless it cannot attach to Electron 44 (Task 3 Step 8), in which case the root pin becomes `1.63.0`. No other new dependency.
- Home server command: `uv run python -m entrypoints.web home` with cwd = repository root (`clients/desktop/../..`). Flags: `--port <VIBESYS_HOME_PORT>` when that variable is set; in dev also `--dev-origin <Vite origin>` and, when `VIBESYS_HOME_PORT` is unset, `--port 8764` (the port `clients/web/vite.config.ts` proxies `/api` to).
- The capability token never appears in argv, the environment, or any log line. Home server stdout (which carries it) is parsed for the one `VibeSys home: <url>` line and never forwarded; stderr is forwarded as `[home] <line>`. Shell diagnostics name origins only (`originOf`), never full URLs.
- Window: `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`, `titleBarStyle: 'hiddenInset'`. The preload exposes exactly `window.vibesysDesktop = {platform}`; there is no IPC channel.
- Exact origins per mode. Prod: the window loads and requests only the home origin `http://127.0.0.1:<port>`. Dev: only the Vite origin `http://127.0.0.1:5173`. WebSockets only to `ws://127.0.0.1:*` (run gateways, Vite HMR). `localhost` is never an app origin (the home server's Host check rejects it). Off-origin navigation, redirects and new windows are denied.
- Quit stops the home server this app launched (SIGINT to its process group, SIGKILL after 5 s). Run servers are started with `start_new_session=True` by the home server and keep running (spec: "detached run servers keep running and are rediscovered on next launch"). A home server that was already running is reused and never signalled.
- One app per state home: when `VIBESYS_STATE_HOME` is set, the Electron profile moves to `$VIBESYS_STATE_HOME/desktop` before the single-instance lock is taken.
- Biome limits: cognitive complexity at most 15, functions at most 80 lines, at most 6 parameters. No new `biome-ignore`.
- Tests: `node:test` API under `bun test`, public functions only, no mocks or monkeypatching (a fake home server process stands in for Python), no sleeps. The e2e smoke test uses the real home server.
- Commands run from `/Users/grootbeat/Documents/vibesys-wt/web-ui/clients` unless stated, with the Bash sandbox disabled (the worktree is outside the write allowlist, tests bind loopback ports, and `pnpm install` downloads the Electron binary from GitHub).
- Commit messages are conventional (`feat(desktop): …`) and end with the session's attribution line (`-m "<the session's attribution line>"` in each commit step stands for it).
- Prose (docs, comments, PR body, UI copy): no em dashes.

## Review Focus

1. The token leaking into a log: `win.loadURL()` rejects with a message that quotes the full URL (`?token=…`), and an unhandled rejection prints it; a fetch error can quote the health URL. Expect every such path caught and reported by origin only. Test: Task 3 smoke test asserts the captured Electron and home output never contains the token or the query of a blocked navigation; Task 2 `stdout, which carries the token, never reaches the stderr sink`.
2. A home server already running (`vibesys web home --open` in a terminal, or one left by an Electron that was killed): `web home` prints that server's URL and exits 0. Expect it reused, not reported as a crash, and never signalled on quit. Test: Task 2 `a launcher that hands over to a running home server is reused, and stop leaves that server alone`.
3. A home server that ignores SIGINT (stuck in a request handler): quit must not hang. Expect SIGKILL to the group after the grace period. Test: Task 2 `stop kills a server that ignores SIGINT after the grace period`.
4. Near-miss origins: `http://localhost:<port>`, the right host on another port, `https`, `wss`, `file:`. Expect all refused for navigation and requests. Test: Task 1 policy tests; Task 3 smoke test navigates to `https://example.com/?token=leak` and stays on the home origin.
5. Launching the app twice: expect the second process to exit 0 and the first to keep one window and one home server. Test: Task 3 smoke test spawns a second Electron with the same state home.

## File Structure

| File | Responsibility |
| --- | --- |
| `clients/desktop/package.json` | `@vibesys/desktop`: scripts, exact Electron pins, `main: out/main/index.cjs` |
| `clients/desktop/tsconfig.json` | Strict typecheck of shell, tests, e2e, configs |
| `clients/desktop/electron.vite.config.ts` | Main and preload as CommonJS; dev renderer = `clients/web`'s Vite config on `127.0.0.1:5173` |
| `clients/desktop/playwright.config.ts` | e2e config (no web server) |
| `clients/desktop/src/main/policy.ts` | Pure rules: app URL, allowed request, permission, home argv, window URL, `originOf` |
| `clients/desktop/src/main/home.ts` | Home server child process lifecycle |
| `clients/desktop/src/main/index.ts` | Electron composition root |
| `clients/desktop/src/preload/index.ts` | `window.vibesysDesktop = {platform}` |
| `clients/desktop/src/main/policy.test.ts`, `home.test.ts` | Unit tests |
| `clients/desktop/test/fake-home.ts` | A process that behaves like `vibesys web home` for `home.test.ts` |
| `clients/desktop/e2e/smoke.spec.ts` | Launch, screenshot, origin guard, second instance, quit, crash |
| `clients/web/src/main.tsx`, `clients/web/src/window.css` | `data-desktop` on `<html>`; drag regions and traffic-light inset |

---

### Task 1: Desktop package, launch and origin policy, architecture registration

**Files:**
- Create: `clients/desktop/package.json`, `clients/desktop/tsconfig.json`, `clients/desktop/src/main/policy.ts`, `clients/desktop/src/main/policy.test.ts`
- Modify: `clients/package.json` (`check:clients`, `test:clients`, `check:ts-architecture`), `clients/knip.jsonc`, `clients/.dependency-cruiser.cjs`, `clients/scripts/check_ts_package_manifests.mjs`, `clients/scripts/check_ts_architecture.test.mjs`, `tests/repoctl/cases.toml`, `clients/pnpm-lock.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces (`clients/desktop/src/main/policy.ts`):
  - `isAppUrl(url: string, origins: readonly string[]): boolean`
  - `isAllowedRequest(url: string, origins: readonly string[]): boolean`
  - `allowPermission(permission: string): boolean`
  - `originOf(url: string): string`
  - `homeArguments(port: string | undefined, devOrigin: string | null): string[]`
  - `windowUrl(home: {readonly origin: string; readonly token: string}, devOrigin: string | null): string`

- [ ] **Step 1: Write the failing architecture tests**

In `clients/scripts/check_ts_architecture.test.mjs`:

Add to `VALID_FILES` (after the `'web/src/index.ts'` entry):

```js
  'desktop/src/main.ts': "import 'node:child_process';\n",
```

Append to `RULE_CASES` (before the closing `];`):

```js
  {
    rule: 'desktop-is-standalone',
    files: {'desktop/src/main.ts': "import '../../web/src/index.js';\n"},
  },
```

In `violatedRules`, add `'desktop/src',` to the cruised directory list after `'web/src',`.

In `manifest policy rejects declared reverse dependencies`, add before `assert.deepEqual`:

```js
  await writeManifest(root, 'desktop', '@vibesys/desktop', {
    '@vibesys/web': 'workspace:*',
  });
```

and append to the expected array:

```js
    'desktop/package.json: @vibesys/desktop must not depend on @vibesys/web',
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pnpm test:ts-architecture`
Expected: FAIL: `desktop-is-standalone rejects desktop/src/main.ts` (no such rule) and the manifest test (missing desktop error).

- [ ] **Step 3: Register the package in the architecture checks**

`clients/.dependency-cruiser.cjs`, first lines:

```js
const PACKAGES = '^(?:backend-client|core-state|tui|web|desktop)/';
const TOOLING = '^(?:tui/dev|tui/benchmarks|core-state/bench|web/e2e|desktop/e2e|desktop/test|scripts)/';
```

Add this rule after `tui-does-not-depend-on-web`:

```js
    {
      // The shell loads the app over HTTP (home server or Vite) and shares no code with the
      // client packages, so a client refactor cannot reach its process and security code.
      name: 'desktop-is-standalone',
      severity: 'error',
      from: {path: '^desktop/'},
      to: {path: ['^(?:backend-client|core-state|tui|web)/', '/node_modules/@vibesys/']},
    },
```

`clients/scripts/check_ts_package_manifests.mjs`, add to `PACKAGES` after `@vibesys/web` (an empty allowlist rejects every workspace dependency, so no forbidden prefix is needed):

```js
  '@vibesys/desktop': {
    directory: 'desktop',
    runtimeWorkspaceDependencies: [],
    forbiddenDependencyPrefixes: [],
  },
```

`tests/repoctl/cases.toml`, case "repoctl code and policy changes select all checks":

```toml
pnpm_packages = ["@vibesys/backend-client", "@vibesys/core-state", "@vibesys/desktop", "@vibesys/tui", "@vibesys/web"]
```

- [ ] **Step 4: Create the package**

`clients/desktop/package.json` (Task 3 adds Electron, the entry point and the launch scripts):

```json
{
  "name": "@vibesys/desktop",
  "productName": "VibeSys",
  "version": "0.1.0",
  "private": true,
  "license": "MIT",
  "type": "module",
  "scripts": {
    "check": "tsc -p tsconfig.json",
    "test": "bun test src"
  },
  "devDependencies": {
    "@types/node": "^24.0.0",
    "typescript": "^5.8.3"
  }
}
```

`clients/desktop/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2023",
    "lib": ["ES2023", "DOM", "DOM.Iterable"],
    "types": ["node"],
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "strict": true,
    "exactOptionalPropertyTypes": true,
    "forceConsistentCasingInFileNames": true,
    "isolatedModules": true,
    "moduleDetection": "force",
    "noFallthroughCasesInSwitch": true,
    "noImplicitOverride": true,
    "noImplicitReturns": true,
    "noPropertyAccessFromIndexSignature": true,
    "noUncheckedIndexedAccess": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "skipLibCheck": true,
    "verbatimModuleSyntax": true,
    "allowImportingTsExtensions": true,
    "noEmit": true
  },
  "include": ["src/**/*.ts", "test/**/*.ts", "e2e/**/*.ts", "electron.vite.config.ts", "playwright.config.ts"]
}
```

(`DOM` is for `page.evaluate` callbacks in e2e; `allowImportingTsExtensions` is for the config's import of `../web/vite.config.ts` in Task 3.)

`clients/knip.jsonc`, add a workspace after `"web"`:

```jsonc
    "desktop": {
      // electron-vite builds the two process entries; `home.test.ts` spawns the fake home by path.
      // Playwright's plugin finds playwright.config.ts and e2e/*.spec.ts.
      "entry": [
        "src/main/index.ts",
        "src/preload/index.ts",
        "src/**/*.test.ts",
        "test/fake-home.ts",
        "electron.vite.config.ts"
      ],
      "project": ["src/**/*.ts", "test/**/*.ts", "e2e/**/*.ts"]
    }
```

`clients/package.json` scripts: append ` desktop/src` to the directory list of `check:ts-architecture` (after `web/src`), append ` && pnpm --filter @vibesys/desktop check` to `check:clients` and ` && pnpm --filter @vibesys/desktop test` to `test:clients`.

Run: `pnpm install`
Expected: the lockfile gains the `desktop` importer; exit 0.

- [ ] **Step 5: Write the failing policy tests**

`clients/desktop/src/main/policy.test.ts`:

```ts
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  allowPermission,
  homeArguments,
  isAllowedRequest,
  isAppUrl,
  originOf,
  windowUrl,
} from './policy.js';

const HOME = 'http://127.0.0.1:8764';
const VITE = 'http://127.0.0.1:5173';

test('the window stays on its exact app origins', () => {
  assert.equal(isAppUrl(`${HOME}/?theme=dark`, [HOME]), true);
  for (const url of [
    'http://localhost:8764/',
    'http://127.0.0.1:8765/',
    'https://127.0.0.1:8764/',
    'https://example.com/',
    'file:///etc/hosts',
    'about:blank',
    'not a url',
  ]) {
    assert.equal(isAppUrl(url, [HOME]), false, url);
  }
  assert.equal(isAppUrl(`${HOME}/`, [VITE]), false);
});

test('requests reach the app origins over HTTP and run gateways over loopback ws only', () => {
  assert.equal(isAllowedRequest(`${HOME}/assets/index.js`, [HOME]), true);
  assert.equal(isAllowedRequest('ws://127.0.0.1:50123/ws?token=t', [HOME]), true);
  for (const url of [
    'ws://localhost:50123/ws',
    'wss://127.0.0.1:50123/ws',
    'http://127.0.0.1:50123/health',
    'https://fonts.googleapis.com/css',
    `${VITE}/src/main.tsx`,
    'not a url',
  ]) {
    assert.equal(isAllowedRequest(url, [HOME]), false, url);
  }
});

test('diagnostics name the origin, never the path or query', () => {
  assert.equal(originOf('https://example.com/a?token=secret'), 'https://example.com');
  assert.equal(originOf('::'), 'an unparseable URL');
});

test('only clipboard writes are permitted', () => {
  assert.equal(allowPermission('clipboard-sanitized-write'), true);
  for (const permission of ['media', 'notifications', 'geolocation', 'openExternal']) {
    assert.equal(allowPermission(permission), false, permission);
  }
});

test('prod passes only an explicit port; dev pins the proxy port and adds the Vite origin', () => {
  assert.deepEqual(homeArguments(undefined, null), []);
  assert.deepEqual(homeArguments('8799', null), ['--port', '8799']);
  assert.deepEqual(homeArguments(undefined, VITE), ['--port', '8764', '--dev-origin', VITE]);
  assert.deepEqual(homeArguments('8799', VITE), ['--port', '8799', '--dev-origin', VITE]);
  assert.deepEqual(homeArguments('', null), []);
  assert.deepEqual(homeArguments('', VITE), ['--port', '8764', '--dev-origin', VITE]);
});

test('the window opens the app on the mode origin with the home capability', () => {
  const home = {origin: HOME, token: 'abc_-9'};
  assert.equal(windowUrl(home, null), `${HOME}/?token=abc_-9`);
  assert.equal(windowUrl(home, VITE), `${VITE}/?token=abc_-9`);
});
```

- [ ] **Step 6: Run them to verify they fail**

Run: `pnpm --filter @vibesys/desktop test`
Expected: FAIL: cannot resolve `./policy.js`.

- [ ] **Step 7: Implement the policy**

`clients/desktop/src/main/policy.ts`:

```ts
/** The shell's security and launch rules, as pure functions of URLs and settings. */

/** `clients/web/vite.config.ts` proxies `/api` here unless `VIBESYS_HOME_PORT` is set. */
const DEV_PROXY_PORT = '8764';
/** The one browser permission the app uses ("Copy run ID"). */
const ALLOWED_PERMISSIONS: ReadonlySet<string> = new Set(['clipboard-sanitized-write']);

/** True when the window may navigate to `url`: its origin is exactly an app origin. */
export function isAppUrl(url: string, origins: readonly string[]): boolean {
  const parsed = URL.parse(url);
  return parsed !== null && origins.includes(parsed.origin);
}

/**
 * True when the page may send this request: HTTP(S) to an app origin, or a WebSocket to a
 * loopback run gateway (the home server's CSP `connect-src ws://127.0.0.1:*`). In dev, where
 * Vite sends no CSP, this is the only such rule.
 */
export function isAllowedRequest(url: string, origins: readonly string[]): boolean {
  const parsed = URL.parse(url);
  if (parsed === null) return false;
  if (parsed.protocol === 'ws:') return parsed.hostname === '127.0.0.1';
  return origins.includes(parsed.origin);
}

export function allowPermission(permission: string): boolean {
  return ALLOWED_PERMISSIONS.has(permission);
}

/** The loggable part of a URL: the query can carry a capability token. */
export function originOf(url: string): string {
  return URL.parse(url)?.origin ?? 'an unparseable URL';
}

/** Flags for `vibesys web home`: the port to listen on, and in dev the Vite origin to accept. */
export function homeArguments(port: string | undefined, devOrigin: string | null): string[] {
  // An empty VIBESYS_HOME_PORT counts as unset.
  const listen = port || (devOrigin === null ? undefined : DEV_PROXY_PORT);
  return [
    ...(listen === undefined ? [] : ['--port', listen]),
    ...(devOrigin === null ? [] : ['--dev-origin', devOrigin]),
  ];
}

/** The app URL with the home capability, on the home origin or (dev) the Vite origin. */
export function windowUrl(
  home: {readonly origin: string; readonly token: string},
  devOrigin: string | null,
): string {
  const url = new URL('/', devOrigin ?? home.origin);
  url.searchParams.set('token', home.token);
  return url.href;
}
```

- [ ] **Step 8: Run the tests and gates**

```bash
pnpm --filter @vibesys/desktop test
pnpm --filter @vibesys/desktop check
pnpm test:ts-architecture
pnpm check:ts-architecture
pnpm check:ts
pnpm check:knip
(cd .. && ./support/repoctl/repoctl verify-policy --cases tests/repoctl/cases.toml)
```

Expected: all exit 0; 6 policy tests pass.

- [ ] **Step 9: Commit**

```bash
git add desktop/package.json desktop/tsconfig.json desktop/src/main/policy.ts desktop/src/main/policy.test.ts \
  package.json pnpm-lock.yaml knip.jsonc .dependency-cruiser.cjs scripts/check_ts_package_manifests.mjs \
  scripts/check_ts_architecture.test.mjs ../tests/repoctl/cases.toml
git commit -m "feat(desktop): add the desktop package with its origin and launch policy" -m "<the session's attribution line>"
```

---

### Task 2: Home server process lifecycle

**Files:**
- Create: `clients/desktop/src/main/home.ts`, `clients/desktop/src/main/home.test.ts`, `clients/desktop/test/fake-home.ts`
- Modify: `clients/package.json` (`check:ts-architecture` gains ` desktop/test`)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces (`clients/desktop/src/main/home.ts`):
  - `interface HomeAddress { readonly origin: string; readonly token: string }`
  - `type HomeEnd = {readonly kind: 'stopped'} | {readonly kind: 'reused'} | {readonly kind: 'crashed'; readonly detail: string}`
  - `interface Home extends HomeAddress { readonly ended: Promise<HomeEnd>; stop(): Promise<void> }`
  - `interface HomeOptions { readonly command: readonly [string, ...string[]]; readonly cwd: string; readonly env: NodeJS.ProcessEnv; readonly readyTimeoutMs: number; readonly stopGraceMs: number; readonly onStderr: (line: string) => void }`
  - `class HomeStartError extends Error` (message: a headline, then the stderr tail, one per line)
  - `parseAnnouncement(line: string): HomeAddress | null`
  - `startHome(options: HomeOptions): Promise<Home>`: resolves once the announced server answers `GET /health?token=`; rejects with `HomeStartError` after the child is killed and reaped.

Behavior the tests pin: the child leads its own process group; `stop()` sends SIGINT to the group, SIGKILL after `stopGraceMs`, resolves after the child is reaped, and is idempotent; the group is signalled only while the child is alive (a reaped leader's pid can be reused); `ended` is `stopped` after `stop()`, `reused` when the child exited 0 and the announced server still answers (plan 2's `run_home` prints a running server's URL and exits 0), else `crashed` with the exit and stderr tail.

- [ ] **Step 1: Write the fake home server**

`clients/desktop/test/fake-home.ts`:

```ts
/**
 * Stands in for `vibesys web home` in src/main/home.test.ts. Modes:
 *   serve           announce, answer /health and /pid, exit 0 on SIGINT (as KeyboardInterrupt does)
 *   stubborn        serve, but ignore SIGINT
 *   unhealthy       serve, but answer /health with 503
 *   silent          serve, but never announce
 *   announce <url>  print a running home server's URL and exit 0 (run_home's reuse path)
 *   fail            write a traceback to stderr and exit 3
 */
import {createServer} from 'node:http';
import type {AddressInfo} from 'node:net';

const [mode = 'serve', url = ''] = process.argv.slice(2);

function serve(): void {
  const server = createServer((request, response) => {
    const path = request.url?.split('?')[0];
    if (path === '/health' && mode === 'unhealthy') response.writeHead(503);
    response.end(path === '/pid' ? String(process.pid) : 'vibesys-ok\n');
  });
  server.listen(0, '127.0.0.1', () => {
    const {port} = server.address() as AddressInfo;
    process.stderr.write(`listening on port ${port}\n`);
    process.stdout.write('a stdout line that is not the announcement: token=fake-token\n');
    if (mode !== 'silent') {
      process.stdout.write(`VibeSys home: http://127.0.0.1:${port}/?token=fake-token\n`);
    }
  });
  process.on('SIGINT', () => {
    if (mode !== 'stubborn') process.exit(0);
  });
}

if (mode === 'announce') {
  process.stdout.write(`VibeSys home: ${url}\n`);
} else if (mode === 'fail') {
  process.stderr.write('Traceback (most recent call last):\nOSError: [Errno 48] Address already in use\n');
  process.exitCode = 3;
} else {
  serve();
}
```

- [ ] **Step 2: Write the failing tests**

`clients/desktop/src/main/home.test.ts`:

```ts
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import type {AddressInfo} from 'node:net';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {type HomeOptions, HomeStartError, parseAnnouncement, startHome} from './home.js';

const FAKE_HOME = fileURLToPath(new URL('../../test/fake-home.ts', import.meta.url));

function options(
  args: string[],
  stderr: string[] = [],
  overrides: Partial<HomeOptions> = {},
): HomeOptions {
  return {
    command: [process.execPath, FAKE_HOME, ...args],
    cwd: process.cwd(),
    env: process.env,
    readyTimeoutMs: 10_000,
    stopGraceMs: 200,
    onStderr: line => stderr.push(line),
    ...overrides,
  };
}

async function answers(origin: string): Promise<boolean> {
  try {
    return (await fetch(`${origin}/health`)).ok;
  } catch {
    return false;
  }
}

function listeningPort(stderr: string[]): string {
  const port = /listening on port (\d+)/.exec(stderr.join('\n'))?.[1];
  assert.ok(port !== undefined, 'the fake reported its port');
  return port;
}

test('only the loopback capability line is an announcement', () => {
  assert.deepEqual(parseAnnouncement('VibeSys home: http://127.0.0.1:8764/?token=a-b_C9'), {
    origin: 'http://127.0.0.1:8764',
    token: 'a-b_C9',
  });
  assert.equal(parseAnnouncement('VibeSys home: http://localhost:8764/?token=abc'), null);
  assert.equal(parseAnnouncement('VibeSys home: http://127.0.0.1:8764/x?token=abc'), null);
  assert.equal(parseAnnouncement('Resolved 212 packages in 3ms'), null);
});

test('a started home answers until stop; stdout, which carries the token, never reaches the stderr sink', async () => {
  const stderr: string[] = [];
  const home = await startHome(options(['serve'], stderr));
  assert.equal(home.token, 'fake-token');
  assert.equal(await answers(home.origin), true);
  await home.stop();
  await home.stop();
  assert.deepEqual(await home.ended, {kind: 'stopped'});
  assert.equal(await answers(home.origin), false);
  assert.ok(stderr.some(line => line.startsWith('listening on port')));
  assert.ok(stderr.every(line => !line.includes('fake-token')));
});

test('stop kills a server that ignores SIGINT after the grace period', async () => {
  const home = await startHome(options(['stubborn']));
  await home.stop();
  assert.deepEqual(await home.ended, {kind: 'stopped'});
  assert.equal(await answers(home.origin), false);
});

test('a server that dies on its own is a crash, reported with its stderr', async () => {
  const home = await startHome(options(['serve']));
  const pid = Number(await (await fetch(`${home.origin}/pid`)).text());
  process.kill(pid, 'SIGKILL');
  const end = await home.ended;
  assert.equal(end.kind, 'crashed');
  assert.match(end.kind === 'crashed' ? end.detail : '', /was killed by SIGKILL\nlistening on port \d+/);
});

test('a launcher that hands over to a running home server is reused, and stop leaves that server alone', async () => {
  const running = createServer((_request, response) => response.end('vibesys-ok\n'));
  await new Promise<void>(resolve => running.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${(running.address() as AddressInfo).port}`;
  try {
    const home = await startHome(options(['announce', `${origin}/?token=theirs`]));
    assert.deepEqual({origin: home.origin, token: home.token}, {origin, token: 'theirs'});
    assert.deepEqual(await home.ended, {kind: 'reused'});
    await home.stop();
    assert.equal(await answers(origin), true);
  } finally {
    running.closeAllConnections();
    running.close();
  }
});

test('a server that exits before announcing fails the start with its stderr tail', async () => {
  await assert.rejects(startHome(options(['fail'])), (error: unknown) => {
    assert.ok(error instanceof HomeStartError);
    assert.match(
      error.message,
      /exited with code 3 before it was ready\nTraceback[\s\S]*Address already in use/,
    );
    return true;
  });
});

test('a server that never announces is killed at the ready timeout', async () => {
  const stderr: string[] = [];
  await assert.rejects(
    startHome(options(['silent'], stderr, {readyTimeoutMs: 500})),
    /not ready within 0.5 s/,
  );
  assert.equal(await answers(`http://127.0.0.1:${listeningPort(stderr)}`), false);
});

test('an announced server that fails its health check is killed and the start fails', async () => {
  const stderr: string[] = [];
  await assert.rejects(startHome(options(['unhealthy'], stderr)), /did not answer \/health/);
  assert.equal(await answers(`http://127.0.0.1:${listeningPort(stderr)}`), false);
});

test('a missing executable fails the start with the command name', async () => {
  await assert.rejects(
    startHome(options([], [], {command: ['vibesys-no-such-home']})),
    /cannot run vibesys-no-such-home/,
  );
});
```

- [ ] **Step 3: Run them to verify they fail**

Run: `pnpm --filter @vibesys/desktop test`
Expected: FAIL: cannot resolve `./home.js`.

- [ ] **Step 4: Implement `home.ts`**

`clients/desktop/src/main/home.ts`:

```ts
/**
 * The `vibesys web home` child process: start it, read its capability from stdout, stop it.
 *
 * Stdout is read for the one announcement line and never forwarded: it carries the token.
 * Stderr (method, path and status per request; tracebacks) is forwarded line by line, and its
 * tail explains a failed start or a crash.
 */
import {type ChildProcessByStdio, spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import type {Readable} from 'node:stream';

/** `_announce` in src/entrypoints/web_home/app.py prints exactly this, once. */
const ANNOUNCEMENT = /^VibeSys home: (http:\/\/127\.0\.0\.1:\d{1,5})\/\?token=([\w-]+)$/;
const TAIL_LINES = 40;
const HEALTH_TIMEOUT_MS = 5_000;

export interface HomeAddress {
  readonly origin: string;
  readonly token: string;
}

/** Why the launched process ended: `stop()`, a hand-over to a running home server, or a crash. */
export type HomeEnd =
  | {readonly kind: 'stopped'}
  | {readonly kind: 'reused'}
  | {readonly kind: 'crashed'; readonly detail: string};

export interface Home extends HomeAddress {
  /** Settles once the launched process has exited. */
  readonly ended: Promise<HomeEnd>;
  /** SIGINT the launched process group, SIGKILL after the grace; resolves once it exited. */
  stop(): Promise<void>;
}

export interface HomeOptions {
  /** The argv; its first element is the executable. */
  readonly command: readonly [string, ...string[]];
  readonly cwd: string;
  readonly env: NodeJS.ProcessEnv;
  readonly readyTimeoutMs: number;
  readonly stopGraceMs: number;
  readonly onStderr: (line: string) => void;
}

/** A start that failed; the message is a headline, then the server's stderr tail. */
export class HomeStartError extends Error {
  override readonly name = 'HomeStartError';
}

interface Exit {
  readonly code: number | null;
  readonly signal: NodeJS.Signals | null;
  readonly error: Error | null;
}

type HomeProcess = ChildProcessByStdio<null, Readable, Readable>;

export function parseAnnouncement(line: string): HomeAddress | null {
  const match = ANNOUNCEMENT.exec(line.trim());
  const origin = match?.[1];
  const token = match?.[2];
  return origin === undefined || token === undefined ? null : {origin, token};
}

export async function startHome(options: HomeOptions): Promise<Home> {
  const child = launch(options);
  const tail: string[] = [];
  createInterface({input: child.stderr}).on('line', line => {
    tail.push(line);
    if (tail.length > TAIL_LINES) tail.shift();
    options.onStderr(line);
  });
  const exit = new Promise<Exit>(resolve => {
    child.once('error', error => resolve({code: null, signal: null, error}));
    child.once('close', (code, signal) => resolve({code, signal, error: null}));
  });
  const signalGroup = (signal: NodeJS.Signals): void => {
    // Only while the leader lives: once it is reaped, its pid and group id can be reused.
    if (child.pid === undefined || child.exitCode !== null || child.signalCode !== null) return;
    try {
      process.kill(-child.pid, signal);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error;
    }
  };
  let address: HomeAddress;
  try {
    address = await announced(child.stdout, exit, options);
    if (!(await answers(address))) {
      throw new HomeStartError(`the home server at ${address.origin} did not answer /health`);
    }
  } catch (error) {
    signalGroup('SIGKILL');
    await exit;
    if (error instanceof HomeStartError) throw new HomeStartError(report(error.message, tail));
    throw error;
  }
  let stopping = false;
  const ended = exit.then(async (end): Promise<HomeEnd> => {
    if (stopping) return {kind: 'stopped'};
    // `vibesys web home` that finds a running home server prints its URL and exits 0.
    if (end.code === 0 && (await answers(address))) return {kind: 'reused'};
    return {kind: 'crashed', detail: report(`the home server ${describe(end)}`, tail)};
  });
  return {
    ...address,
    ended,
    async stop() {
      stopping = true;
      signalGroup('SIGINT');
      const escalate = setTimeout(() => signalGroup('SIGKILL'), options.stopGraceMs);
      await ended;
      clearTimeout(escalate);
    },
  };
}

function launch(options: HomeOptions): HomeProcess {
  const [executable, ...args] = options.command;
  try {
    // detached: the child leads a new process group, so one signal reaches `uv` and Python.
    // Run servers start their own sessions (start_new_session) and outlive it by design.
    return spawn(executable, args, {
      cwd: options.cwd,
      env: options.env,
      detached: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (error) {
    throw new HomeStartError(`cannot run ${executable}: ${(error as Error).message}`);
  }
}

function announced(
  stdout: Readable,
  exit: Promise<Exit>,
  options: HomeOptions,
): Promise<HomeAddress> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      const seconds = options.readyTimeoutMs / 1000;
      reject(new HomeStartError(`the home server was not ready within ${seconds} s`));
    }, options.readyTimeoutMs);
    createInterface({input: stdout}).on('line', line => {
      const address = parseAnnouncement(line);
      if (address === null) return;
      clearTimeout(timer);
      resolve(address);
    });
    void exit.then(end => {
      clearTimeout(timer);
      reject(
        new HomeStartError(
          end.error === null
            ? `the home server ${describe(end)} before it was ready`
            : `cannot run ${options.command[0]}: ${end.error.message}`,
        ),
      );
    });
  });
}

async function answers({origin, token}: HomeAddress): Promise<boolean> {
  try {
    const response = await fetch(`${origin}/health?token=${encodeURIComponent(token)}`, {
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });
    return response.ok && (await response.text()) === 'vibesys-ok\n';
  } catch {
    // A fetch error can quote the URL, which carries the token; callers report the origin.
    return false;
  }
}

function describe(end: Exit): string {
  return end.signal === null ? `exited with code ${end.code ?? 'unknown'}` : `was killed by ${end.signal}`;
}

function report(headline: string, tail: readonly string[]): string {
  return [headline, ...tail].join('\n');
}
```

`clients/package.json`: append ` desktop/test` to `check:ts-architecture`'s directory list.

- [ ] **Step 5: Run the tests and gates**

```bash
pnpm --filter @vibesys/desktop test
pnpm --filter @vibesys/desktop check
pnpm check:ts
pnpm check:ts-architecture
pnpm check:knip
```

Expected: all exit 0; 15 tests pass (6 policy, 9 home).

- [ ] **Step 6: Commit**

```bash
git add desktop/src/main/home.ts desktop/src/main/home.test.ts desktop/test/fake-home.ts package.json
git commit -m "feat(desktop): start, health-check, stop, and classify the home server process" -m "<the session's attribution line>"
```

---

### Task 3: Electron main process, preload, dev and prod entries, e2e smoke

**Files:**
- Create: `clients/desktop/src/main/index.ts`, `clients/desktop/src/preload/index.ts`, `clients/desktop/electron.vite.config.ts`, `clients/desktop/playwright.config.ts`, `clients/desktop/e2e/smoke.spec.ts`
- Modify: `clients/desktop/package.json`, `clients/package.json`, `clients/pnpm-workspace.yaml`, `clients/pnpm-lock.yaml`, `.gitignore`

**Interfaces:**
- Consumes: Task 1 `isAppUrl`, `isAllowedRequest`, `allowPermission`, `originOf`, `homeArguments`, `windowUrl`; Task 2 `startHome`, `Home`, `HomeEnd`, `HomeStartError`.
- Produces:
  - `pnpm desktop` (dev: Vite HMR) and `pnpm desktop:start` (built bundle), run from `clients/`.
  - `window.vibesysDesktop: {readonly platform: NodeJS.Platform}` in every page of the window (Task 4 reads it).
  - Stderr lines the e2e test waits on: `vibesys-desktop: blocked navigation to <origin>`, `vibesys-desktop: the home server stopped unexpectedly (<first line>)`.
  - Build output `clients/desktop/out/main/index.cjs`, `clients/desktop/out/preload/index.cjs`.

- [ ] **Step 1: Add Electron and the scripts**

`clients/pnpm-workspace.yaml`, under `allowBuilds:` (Electron's postinstall downloads its binary):

```yaml
  electron: true
```

`clients/desktop/package.json` becomes:

```json
{
  "name": "@vibesys/desktop",
  "productName": "VibeSys",
  "version": "0.1.0",
  "private": true,
  "license": "MIT",
  "type": "module",
  "main": "out/main/index.cjs",
  "scripts": {
    "dev": "electron-vite dev",
    "build": "electron-vite build",
    "start": "electron-vite build && electron .",
    "check": "tsc -p tsconfig.json",
    "test": "bun test src",
    "test:e2e": "pnpm --filter @vibesys/web build && electron-vite build && playwright test"
  },
  "devDependencies": {
    "@types/node": "^24.0.0",
    "electron": "44.4.5",
    "electron-vite": "5.0.0",
    "typescript": "^5.8.3"
  }
}
```

`dev` deliberately has no `--watch`: electron-vite would kill and restart Electron on every main-process rebuild while the old instance still holds the single-instance lock and is stopping its home server. Main and preload edits need a restart; `clients/web` edits hot-reload.

`clients/package.json` scripts, add:

```json
    "desktop": "pnpm --filter @vibesys/desktop dev",
    "desktop:start": "pnpm --filter @vibesys/web build && pnpm --filter @vibesys/desktop start",
```

and append ` desktop/e2e` to `check:ts-architecture`'s directory list.

`.gitignore`, after `clients/web/test-results/`:

```
clients/desktop/out/
clients/desktop/test-results/
```

Run: `pnpm install`
Expected: exit 0; `node -e "console.log(require('electron'))"` (from `clients/desktop`) prints a path to `Electron.app/Contents/MacOS/Electron` that exists.

- [ ] **Step 2: Write the failing e2e smoke test**

`clients/desktop/playwright.config.ts`:

```ts
import {defineConfig} from '@playwright/test';

export default defineConfig({testDir: './e2e', timeout: 120_000, workers: 1});
```

`clients/desktop/e2e/smoke.spec.ts`:

```ts
import {spawn} from 'node:child_process';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {type AddressInfo, createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {_electron, type ElectronApplication, expect, test} from '@playwright/test';

const DESKTOP = fileURLToPath(new URL('..', import.meta.url));
const ELECTRON = createRequire(import.meta.url)('electron') as string;

test.describe.configure({mode: 'serial'});

interface Launched {
  readonly app: ElectronApplication;
  readonly env: Record<string, string>;
  readonly origin: string;
  readonly stateHome: string;
  output(): string;
  waitForOutput(pattern: RegExp): Promise<void>;
}

async function freePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const {port} = server.address() as AddressInfo;
  await new Promise<void>(resolve => server.close(() => resolve()));
  return port;
}

/** The built app on a fresh state home and port, so it never reuses the developer's home server. */
async function launch(): Promise<Launched> {
  const stateHome = await mkdtemp(join(tmpdir(), 'vibesys-desktop-'));
  const port = await freePort();
  const env: Record<string, string> = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (value !== undefined && name !== 'ELECTRON_RENDERER_URL') env[name] = value;
  }
  env['VIBESYS_STATE_HOME'] = stateHome;
  env['VIBESYS_HOME_PORT'] = String(port);
  const app = await _electron.launch({executablePath: ELECTRON, args: [DESKTOP], env});
  let text = '';
  const waiters = new Set<() => void>();
  for (const stream of [app.process().stdout, app.process().stderr]) {
    stream?.on('data', (chunk: Buffer) => {
      text += chunk.toString();
      for (const wake of waiters) wake();
    });
  }
  return {
    app,
    env,
    origin: `http://127.0.0.1:${port}`,
    stateHome,
    output: () => text,
    waitForOutput: pattern =>
      new Promise(resolve => {
        const wake = () => {
          if (!pattern.test(text)) return;
          waiters.delete(wake);
          resolve();
        };
        waiters.add(wake);
        wake();
      }),
  };
}

async function homePid(stateHome: string): Promise<number> {
  const record = JSON.parse(await readFile(join(stateHome, 'web', 'home.json'), 'utf8'));
  return (record as {pid: number}).pid;
}

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

test('one launch starts the home server and shows the app; it stays on its origin; quit stops the server', async () => {
  const launched = await launch();
  const {app, origin, stateHome} = launched;
  const page = await app.firstWindow();
  await page.locator('#root > *').first().waitFor();
  const url = new URL(page.url());
  expect(url.origin).toBe(origin);
  const token = url.searchParams.get('token') ?? '';
  expect(token.length).toBeGreaterThan(20);
  // Sandboxed, isolated renderer: no Node globals; the preload exposes one constant.
  expect(
    await page.evaluate(() => [
      typeof Reflect.get(window, 'require'),
      typeof Reflect.get(window, 'process'),
      Object.keys((Reflect.get(window, 'vibesysDesktop') as object | undefined) ?? {}),
    ]),
  ).toEqual(['undefined', 'undefined', ['platform']]);
  await page.screenshot({path: test.info().outputPath('window.png')});

  const refused = launched.waitForOutput(/blocked navigation to https:\/\/example\.com/);
  await page.evaluate(() => {
    window.location.href = 'https://example.com/?token=leak';
  });
  await refused;
  expect(new URL(page.url()).origin).toBe(origin);
  expect(await page.evaluate(() => window.open('https://example.com/') === null)).toBe(true);
  expect(app.windows()).toHaveLength(1);

  const second = spawn(ELECTRON, [DESKTOP], {env: launched.env, stdio: 'ignore'});
  expect(await new Promise(resolve => second.once('exit', resolve))).toBe(0);
  expect(app.windows()).toHaveLength(1);

  const pid = await homePid(stateHome);
  await app.close();
  expect(alive(pid)).toBe(false);
  await expect(readFile(join(stateHome, 'web', 'home.json'))).rejects.toThrow();
  expect(launched.output()).not.toContain(token);
  expect(launched.output()).not.toContain('leak');
  await rm(stateHome, {recursive: true, force: true});
});

test('a home server crash is detected and reported', async () => {
  const launched = await launch();
  await launched.app.firstWindow();
  const pid = await homePid(launched.stateHome);
  const reported = launched.waitForOutput(/the home server stopped unexpectedly/);
  process.kill(pid, 'SIGKILL');
  await reported;
  // The crash dialog is open and the server is gone, so nothing is left to stop.
  launched.app.process().kill('SIGKILL');
  await rm(launched.stateHome, {recursive: true, force: true});
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `pnpm --filter @vibesys/desktop test:e2e`
Expected: FAIL: `electron-vite build` reports no config or entry (`electron.vite.config.ts` and `src/main/index.ts` do not exist yet).

- [ ] **Step 4: Write the electron-vite config**

`clients/desktop/electron.vite.config.ts`:

```ts
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {defineConfig} from 'electron-vite';
import web from '../web/vite.config.ts';

const WEB_ROOT = fileURLToPath(new URL('../web', import.meta.url));

/** Sandboxed preloads must be CommonJS; main is built the same way, next to it. */
function commonJs(entry: string) {
  return {lib: {entry, formats: ['cjs' as const], fileName: () => 'index.cjs'}};
}

export default defineConfig(({command}) => ({
  main: {build: commonJs('src/main/index.ts')},
  preload: {build: commonJs('src/preload/index.ts')},
  // `electron-vite dev` serves clients/web with its own Vite config (aliases, the /api proxy)
  // and hot reload, on the origin the home server accepts through --dev-origin. The built app
  // is clients/web/dist, served by the home server, so `build` has no renderer.
  ...(command === 'serve'
    ? {
        renderer: {
          ...web,
          root: WEB_ROOT,
          server: {...web.server, host: '127.0.0.1', port: 5173, strictPort: true},
          // electron-vite validates the renderer input in serve mode too, and defaults it to
          // src/renderer/index.html under this package, not `root`.
          build: {rollupOptions: {input: join(WEB_ROOT, 'index.html')}},
        },
      }
    : {}),
}));
```

- [ ] **Step 5: Write the preload**

`clients/desktop/src/preload/index.ts`:

```ts
import {contextBridge} from 'electron';

// The page's whole view of the shell: the platform drawing the window chrome, so the app can
// inset the macOS traffic lights. contextBridge hands the page a copy, so it cannot write back.
// There is no IPC channel, hence no sender to validate.
contextBridge.exposeInMainWorld('vibesysDesktop', {platform: process.platform});
```

- [ ] **Step 6: Write the main process**

`clients/desktop/src/main/index.ts`:

```ts
/**
 * Electron main process: one window over the app that `vibesys web home` serves.
 *
 * Owns the home server it starts (stopped on quit; a crash offers a restart), confines the
 * window to the app origin, and keeps the capability token out of every log line.
 */
import {join, resolve} from 'node:path';
import {
  app,
  BrowserWindow,
  dialog,
  Menu,
  type MenuItemConstructorOptions,
  nativeTheme,
  session,
  type WebContents,
} from 'electron';
import {type Home, type HomeEnd, startHome} from './home.js';
import {
  allowPermission,
  homeArguments,
  isAllowedRequest,
  isAppUrl,
  originOf,
  windowUrl,
} from './policy.js';

/** `clients/desktop` is the app path; the home server runs from the checkout around it. */
const REPOSITORY = resolve(app.getAppPath(), '../..');
/** Main is built as CommonJS next to the preload (electron.vite.config.ts). */
const PRELOAD = join(__dirname, '../preload/index.cjs');
/** Set by `electron-vite dev` to the Vite server of clients/web; unset for the built app. */
const DEV_URL = process.env['ELECTRON_RENDERER_URL'];
const DEV_ORIGIN = DEV_URL === undefined ? null : new URL(DEV_URL).origin;
/** The first `uv run` in a checkout syncs its environment before the server starts. */
const READY_TIMEOUT_MS = 60_000;
const STOP_GRACE_MS = 5_000;

/** The latest launch; null when it failed. Quit waits for it, so a starting server is stopped too. */
let home: Promise<Home | null> = Promise.resolve(null);
let win: BrowserWindow | null = null;
/** Where the window may navigate and send requests: the home server, or Vite in dev. */
let appOrigins: readonly string[] = [];
let quitting = false;

function log(message: string): void {
  console.error(`vibesys-desktop: ${message}`);
}

function guard(contents: WebContents): void {
  const stayInApp = (event: {readonly url: string; preventDefault(): void}): void => {
    if (isAppUrl(event.url, appOrigins)) return;
    event.preventDefault();
    log(`blocked navigation to ${originOf(event.url)}`);
  };
  contents.on('will-navigate', stayInApp);
  contents.on('will-redirect', stayInApp);
  contents.setWindowOpenHandler(({url}) => {
    log(`blocked a new window for ${originOf(url)}`);
    return {action: 'deny'};
  });
}

function secureSession(): void {
  const {webRequest} = session.defaultSession;
  const urls = ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'];
  webRequest.onBeforeRequest({urls}, (details, callback) => {
    const cancel = !isAllowedRequest(details.url, appOrigins);
    if (cancel) log(`blocked a request to ${originOf(details.url)}`);
    callback({cancel});
  });
  session.defaultSession.setPermissionRequestHandler((_contents, permission, callback) =>
    callback(allowPermission(permission)),
  );
  session.defaultSession.setPermissionCheckHandler((_contents, permission) =>
    allowPermission(permission),
  );
}

function menu(): Menu {
  const first: MenuItemConstructorOptions =
    process.platform === 'darwin' ? {role: 'appMenu'} : {role: 'fileMenu'};
  return Menu.buildFromTemplate([first, {role: 'editMenu'}, {role: 'viewMenu'}, {role: 'windowMenu'}]);
}

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    show: false,
    title: 'VibeSys',
    titleBarStyle: 'hiddenInset',
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#111113' : '#fcfcfd',
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });
  window.once('ready-to-show', () => window.show());
  window.on('closed', () => {
    win = null;
  });
  return window;
}

async function launch(): Promise<Home | null> {
  try {
    return await startHome({
      command: [
        'uv',
        'run',
        'python',
        '-m',
        'entrypoints.web',
        'home',
        ...homeArguments(process.env['VIBESYS_HOME_PORT'], DEV_ORIGIN),
      ],
      cwd: REPOSITORY,
      env: process.env,
      readyTimeoutMs: READY_TIMEOUT_MS,
      stopGraceMs: STOP_GRACE_MS,
      onStderr: line => console.error(`[home] ${line}`),
    });
  } catch (error) {
    // A HomeStartError holds a headline and the server's stderr tail; neither carries the token.
    const detail = error instanceof Error ? error.message : String(error);
    log(detail.split('\n', 1)[0] ?? detail);
    dialog.showErrorBox('VibeSys could not start its home server', detail);
    app.quit();
    return null;
  }
}

async function openHome(): Promise<void> {
  home = launch();
  const current = await home;
  if (current === null || quitting) return;
  void current.ended.then(onHomeEnded);
  appOrigins = [DEV_ORIGIN ?? current.origin];
  win ??= createWindow();
  // The rejection message quotes the URL, which carries the token: report the origin only.
  win
    .loadURL(windowUrl(current, DEV_ORIGIN))
    .catch(() => log(`could not load the app from ${appOrigins[0]}`));
}

async function onHomeEnded(end: HomeEnd): Promise<void> {
  switch (end.kind) {
    case 'stopped':
      return;
    case 'reused':
      // A home server started elsewhere (e.g. `vibesys web home --open`) rejects Vite's writes
      // with 403 forbidden_origin unless it was started with --dev-origin.
      if (DEV_ORIGIN !== null) {
        log('reusing a home server that was started without --dev-origin; writes from Vite fail until it is restarted');
      }
      return;
    case 'crashed': {
      log(`the home server stopped unexpectedly (${end.detail.split('\n', 1)[0]})`);
      const {response} = await dialog.showMessageBox({
        type: 'error',
        message: 'The VibeSys home server stopped.',
        detail: end.detail,
        buttons: ['Restart', 'Quit'],
        defaultId: 0,
        cancelId: 1,
      });
      if (response === 0 && !quitting) await openHome();
      else app.quit();
    }
  }
}

function main(): void {
  for (const signal of ['SIGINT', 'SIGTERM'] as const) process.on(signal, () => app.quit());
  app.on('second-instance', () => {
    if (win === null) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });
  app.on('web-contents-created', (_event, contents) => guard(contents));
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', event => {
    if (quitting) return;
    quitting = true;
    event.preventDefault();
    void home.then(current => current?.stop()).finally(() => app.quit());
  });
  void app.whenReady().then(() => {
    secureSession();
    Menu.setApplicationMenu(menu());
    return openHome();
  });
}

// One app per state home, as there is one home server per state home.
const stateHome = process.env['VIBESYS_STATE_HOME'];
if (stateHome !== undefined && stateHome !== '') app.setPath('userData', join(stateHome, 'desktop'));
if (app.requestSingleInstanceLock()) main();
else app.quit();
```

- [ ] **Step 7: Build and check the output names**

Run: `pnpm --filter @vibesys/desktop build && ls desktop/out/main desktop/out/preload`
Expected: `index.cjs` in each. If electron-vite names them otherwise, fix `commonJs()` in the config, not `PRELOAD` or `main`.

- [ ] **Step 8: Run the e2e test to verify it passes**

Run: `pnpm --filter @vibesys/desktop test:e2e`
Expected: 2 passed. A window opens on screen for each test. Open `desktop/test-results/*/window.png` and confirm it shows the app (sidebar and title row), not an error body. If Playwright 1.55 cannot attach to Electron 44 (a launch timeout before `firstWindow`), bump the root `@playwright/test` pin in `clients/package.json` to `1.63.0`, run `pnpm install`, rerun `pnpm --filter @vibesys/web test:e2e` and this test, add `package.json` to this task's commit, and continue.

- [ ] **Step 9: Run the gates**

```bash
pnpm --filter @vibesys/desktop test
pnpm --filter @vibesys/desktop check
pnpm check:ts
pnpm check:ts-architecture
pnpm check:knip
```

Expected: all exit 0.

- [ ] **Step 10: Commit**

```bash
git add desktop/package.json desktop/electron.vite.config.ts desktop/playwright.config.ts \
  desktop/src/main/index.ts desktop/src/preload/index.ts desktop/e2e/smoke.spec.ts \
  package.json pnpm-workspace.yaml pnpm-lock.yaml ../.gitignore
git commit -m "feat(desktop): Electron shell that launches the home server and opens the app" -m "<the session's attribution line>"
```

---

### Task 4: macOS window chrome: traffic-light inset and drag regions

Requires plan 3 in the worktree (`clients/web/src/window.css` with `.win`, `.sidehead`, `.titlebar`).

**Files:**
- Modify: `clients/desktop/src/main/index.ts` (`createWindow`), `clients/web/src/main.tsx`, `clients/web/src/window.css`, `clients/desktop/e2e/smoke.spec.ts`

**Interfaces:**
- Consumes: Task 3 `window.vibesysDesktop.platform`.
- Produces: `<html data-desktop="<platform>">` in the desktop window only (browser mode is unchanged).

- [ ] **Step 1: Extend the smoke test (failing)**

In `smoke.spec.ts`, first test, after the Node-globals assertion add:

```ts
  expect(await page.evaluate(() => document.documentElement.dataset['desktop'])).toBe(
    process.platform,
  );
```

Run: `pnpm --filter @vibesys/desktop test:e2e`
Expected: FAIL: received `undefined`.

- [ ] **Step 2: Mark the document in desktop mode**

`clients/web/src/main.tsx`, directly after the `?theme=` block:

```ts
// The desktop shell (clients/desktop) exposes its platform; window.css insets the native chrome.
const desktop = (window as Window & {vibesysDesktop?: {platform: string}}).vibesysDesktop;
if (desktop !== undefined) document.documentElement.dataset['desktop'] = desktop.platform;
```

- [ ] **Step 3: Drag regions and the inset**

Append to `clients/web/src/window.css`:

```css
/* desktop shell (clients/desktop): the 40px sidebar head and title row move the window. On macOS
   the traffic lights sit over the sidebar head (x 16, centred at y 20), or over the title row
   while the sidebar is hidden, which then starts after them. */
:root[data-desktop] :is(.sidehead, .titlebar) { -webkit-app-region: drag; user-select: none; }
:root[data-desktop] :is(.sidehead, .titlebar) :is(button, a, input, select, textarea, [role='menu'], [tabindex]) { -webkit-app-region: no-drag; }
:root[data-desktop='darwin'] .win:not(:has(> .side)) .titlebar { padding-left: 80px; }
```

- [ ] **Step 4: Position the traffic lights**

In `clients/desktop/src/main/index.ts` `createWindow`, after `titleBarStyle: 'hiddenInset',` add:

```ts
    // The mockup's 40px sidebar head: 16px from the left edge, circles centred at y = 20.
    trafficLightPosition: {x: 16, y: 13},
```

- [ ] **Step 5: Run the tests**

```bash
pnpm --filter @vibesys/desktop test:e2e
pnpm --filter @vibesys/web test
pnpm --filter @vibesys/web test:e2e
pnpm --filter @vibesys/web check
```

Expected: all pass (browser mode never sets `data-desktop`, so web screenshots are unchanged).

- [ ] **Step 6: Look at the real window chrome**

Playwright screenshots show only web content, not the native traffic lights. Write this scratch script to `clients/desktop/chrome-shot.mjs` (do not commit it):

```js
import {execFileSync} from 'node:child_process';
import {_electron} from '@playwright/test';

const app = await _electron.launch({args: ['.']});
const page = await app.firstWindow();
await page.locator('.titlebar').waitFor();
const shoot = async name => {
  const b = await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].getBounds());
  execFileSync('screencapture', ['-x', '-o', '-R', `${b.x},${b.y},${b.width},${b.height}`, name]);
};
await shoot('/tmp/vibesys-chrome-sidebar.png');
await page.getByRole('button', {name: 'Hide sidebar'}).click();
await shoot('/tmp/vibesys-chrome-no-sidebar.png');
await app.close();
```

Run from `clients/desktop`: `node chrome-shot.mjs`, then open both PNGs and compare with `docs/superpowers/specs/2026-09-28-web-app/live-dark.png`. Expected: the three circles start about 16px from the left edge with centres on the vertical middle of the 40px sidebar head; with the sidebar hidden, "Show sidebar" sits right of the circles without overlap; dragging the title row moves the window and its buttons still click. Adjust `trafficLightPosition.y` (and the 80px) until they match. If the PNGs show only the desktop wallpaper, the terminal lacks Screen Recording permission: grant it, or capture the window by hand (Cmd-Shift-4, Space). Delete `chrome-shot.mjs` afterwards.

- [ ] **Step 7: Commit**

```bash
git add desktop/src/main/index.ts desktop/e2e/smoke.spec.ts web/src/main.tsx web/src/window.css
git commit -m "feat(desktop): inset the macOS traffic lights and make the title rows draggable" -m "<the session's attribution line>"
```

---

### Task 5: Docs, full gates, dev-mode check, and PR

**Files:**
- Modify: `docs/contributing/web-development.md`

- [ ] **Step 1: Document the desktop app**

Append to `docs/contributing/web-development.md`:

```md
## Desktop app

`clients/desktop` is an Electron shell. It starts `vibesys web home` from this checkout and
opens the app in a native window. Run these from `clients/`:

| Command | What runs |
| --- | --- |
| `pnpm desktop` | Vite with hot reload on `http://127.0.0.1:5173`, and the home server with `--dev-origin` for it. Edits under `clients/web` reload in place; main-process and preload edits need a restart. A home server that was already running without `--dev-origin` is reused, and writes from Vite fail until it is restarted. |
| `pnpm desktop:start` | Builds `clients/web` and the shell, then loads the built app from the home server. |
| `pnpm --filter @vibesys/desktop test:e2e` | Launches the built app with Playwright on a temporary state home and screenshots the window. Needs `uv`. |

`VIBESYS_HOME_PORT` sets the home server's port (the Vite proxy reads it too).
`VIBESYS_STATE_HOME` moves the home server's state and the shell's profile; one app runs per
state home. Quitting stops the home server the app started; run servers keep running and are
listed again on the next launch. A home server that was already running (for example
`vibesys web home --open`) is reused and left running. There is no packaged build yet.
```

Run (repository root): `uv run python scripts/check_doc_links.py`
Expected: exit 0.

- [ ] **Step 2: Full gates**

From `clients/`:

```bash
pnpm check:ts
pnpm check:ts-architecture
pnpm test:ts-architecture
pnpm check:knip
pnpm check:clients
pnpm test:clients
pnpm --filter @vibesys/desktop test:e2e
pnpm --filter @vibesys/web test:e2e
```

From the repository root: `./support/repoctl/repoctl verify-policy --cases tests/repoctl/cases.toml`.
Expected: every command exits 0.

- [ ] **Step 3: Dev mode by hand**

Run `pnpm desktop` from `clients/`. Expected: the window shows the app from `http://127.0.0.1:5173`; changing a visible string in `clients/web/src/ui/TitleRow.tsx` updates the window without a reload (revert the edit). Press Ctrl-C in the terminal. Expected: Electron exits, `$VIBESYS_STATE_HOME/web/home.json` (default `~/.vibesys/web/home.json`) is gone, and `lsof -nP -iTCP:8764 -sTCP:LISTEN` prints nothing. Then `pnpm desktop:start`, Cmd-Q, same expectations. Run `pnpm desktop:start` twice: the second invocation exits and the first window comes to the front.

- [ ] **Step 4: Open the PR**

Use the `open-pr` skill. Title: `feat(desktop): Electron shell that launches the home server and the app`. The branch depends on plan 2's group (a) PR (the `vibesys web home` auth contract) and on plan 3 (a loadable app); stack it on the topmost of those branches and name both dependencies in the body. Attach `window.png` from the e2e run and the two chrome screenshots from Task 4 Step 6.

---

## Self-Review

| Spec item (section 6 and Testing) | Task |
| --- | --- |
| `clients/desktop`, main plus preload, `electron-vite`; added to the architecture checks' package list | 1 (package, depcruise, manifest policy, knip, repoctl), 3 (electron-vite) |
| Main starts `uv run python -m entrypoints.web home`, keeps the capability, loads the app in a `BrowserWindow` with `hiddenInset` | 2 (`startHome`), 3 (`launch`, `openHome`, `createWindow`) |
| Dev loads the Vite dev server for hot reload | 3 (`renderer` in serve mode, `DEV_ORIGIN`, `--dev-origin`) |
| `contextIsolation`, `sandbox: true`, no Node integration | 3, asserted in the smoke test |
| CSP header from the home server | Plan 2 (landed `_CSP`); the shell adds no CSP of its own and loads prod from the home origin, so the header applies |
| Narrow preload API with sender validation | 3: the preload exposes one constant and no IPC channel exists (see resolution 2) |
| Exact allowed origins per mode; deny external navigation and new windows; never forward the capability off-origin | 1 (`isAppUrl`, `isAllowedRequest`), 3 (`guard`, `secureSession`), smoke test |
| Single instance, a menu | 3 (`requestSingleInstanceLock`, `second-instance`, `menu()`), smoke test |
| Quitting stops the home server; detached run servers keep running | 2 (`stop`, own process group), 3 (`before-quit`), smoke test (pid gone, `home.json` removed) |
| Smoke test: starts the home server, loads the UI, refuses off-origin navigation | 3 |
| Native macOS title bar matching the mockup | 4 |
| Existing gates stay green | 5 |

Placeholders: none; every code step has the code that ships. Names are consistent across tasks: `startHome`, `Home`, `HomeEnd`, `HomeStartError`, `homeArguments`, `windowUrl`, `isAppUrl`, `isAllowedRequest`, `allowPermission`, `originOf`, `window.vibesysDesktop`. Review Focus: each item names its test in its owning task.

## Spec ambiguities resolved

1. **Runs on quit.** The spec keeps detached run servers alive across quit and rediscovers them; the shell stops only the home server's process group, and the home server already starts runs with `start_new_session=True`, so they are unaffected. Killing live runs on quit would drop work the reopen and resume design exists to keep.
2. **Preload and sender validation.** The app needs nothing from the main process at runtime, so there is no IPC channel and no sender to validate. The preload exposes one constant (`platform`, for the traffic-light inset). Navigation, redirect, new-window and request guards carry the origin checks instead.
3. **Token handoff.** The window opens `<app origin>/?token=<token>`, exactly the browser-mode capability URL (the home server requires `?token=` for `index.html`). In dev the same query goes on the Vite URL. The token never enters argv or the environment; stdout is parsed, not forwarded; every diagnostic names an origin only. How the app uses the token after load is plan 4's.
4. **Electron origin.** There is none of its own: prod pages are served by the home server on `http://127.0.0.1:<port>`, dev pages by Vite on `http://127.0.0.1:5173` (passed as `--dev-origin`). No `file://` or custom scheme, so the home server's Host and Origin allowlists need no change.
5. **A home server already running** (browser mode, or one orphaned when Electron was killed) is reused and never signalled; the shell does not watch it for crashes. A parent-death watch in `web home` would remove orphans; not added (the next launch reuses the orphan).
6. **Dev port.** In dev the shell passes `--port 8764` unless `VIBESYS_HOME_PORT` is set, so the home server and the Vite `/api` proxy agree by construction. This rewrites a different saved port in dev only.
7. **Last window closed** quits the app (and stops the home server) on every platform, instead of the macOS dock convention; there is one window.
8. **Dev has no CSP** (Vite's React preamble is inline); the request filter enforces the same origins in both modes.
9. **Crash handling.** A native dialog offers Restart (new home server, new token, window reloaded) or Quit. The e2e test asserts detection; the Restart path is checked by hand.
