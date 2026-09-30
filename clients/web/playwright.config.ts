import {defineConfig} from '@playwright/test';

/**
 * Browser e2e configuration for the web viewer.
 *
 * Every CI-facing value is stated here rather than inherited from Playwright's
 * defaults, because the cost and the failure modes of this suite come from the
 * detached gateway the specs boot, not from anything Playwright can guess.
 *
 * `CI` is set by GitHub Actions and reaches this file unchanged: the `tui_e2e`
 * group runs through `repoctl`, which inherits the process environment.
 */
const inCI = !!process.env['CI'];

export default defineConfig({
  testDir: './e2e',
  // Flakiness is a defect to fix, not to absorb. A retry would convert a
  // load-sensitive gateway boot into a green run and erase the only signal
  // that it is load-sensitive. See #1034 for the live instance of that class.
  retries: 0,
  // A stray `test.only` silently narrows CI to a green subset. Locally it is
  // the normal way to iterate on one spec.
  forbidOnly: inCI,
  // The gateway specs pass `--web-port 0`, so parallel workers would not
  // collide on a port. They would multiply a Python interpreter and a Chromium
  // across the runner's 4 vCPUs while each spec races a fixed per-test
  // deadline, which makes the deadline a property of the scheduler. A
  // three-spec suite has no wall-clock time worth buying with that.
  workers: 1,
  // A gateway spec pays two synchronous `uv run` starts, measured at ~2.8 s
  // each for the `entrypoints.server` import graph, wrapped around up to
  // `_DETACHED_START_TIMEOUT_SECONDS` (10 s, src/entrypoints/server.py) of
  // readiness polling: ~15.6 s of the 30 s default before the first browser
  // assertion. Global rather than per spec because any narrower scope needs a
  // hand-kept list of which specs boot a gateway, and a new one would silently
  // inherit the short budget. Vite's startup is not in this budget; `webServer`
  // has its own timeout and the suite waits for readiness first.
  timeout: 60_000,
  webServer: {
    command: 'pnpm dev --host 127.0.0.1',
    port: 5173,
    // Reuse the `pnpm dev` a developer already has running. In CI a listener
    // on 5173 is a stale process, not the build under test.
    reuseExistingServer: !inCI,
  },
  use: {baseURL: 'http://127.0.0.1:5173'},
});
