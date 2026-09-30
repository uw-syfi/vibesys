import {execFileSync, spawnSync} from 'node:child_process';
import {copyFileSync, existsSync, mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {expect, test} from '@playwright/test';

/**
 * Detached live-gateway lifecycle for browser specs.
 *
 * A spec gets one capability URL and a `stop` that is safe to call twice, so
 * the spec never needs to know about the instance record, the runtime
 * directory, or the `entrypoints.server` argument list.
 *
 * `stop` returns the stop command's exit status so a spec can assert that the
 * detached process really went away, and never throws, so calling it from
 * `finally` cannot replace a real assertion failure with a teardown error.
 * Anything that went wrong during teardown is reported in `errors`.
 */
interface GatewayStop {
  /** Exit status of `entrypoints.web stop`, or null if it did not run. */
  readonly status: number | null;
  /** Teardown failures, suppressed so they cannot mask a body failure. */
  readonly errors: readonly string[];
}

export interface LiveGateway {
  readonly url: string;
  /** Detached gateway process, so a spec can assert it really went away. */
  readonly pid: number;
  stop(): GatewayStop;
}

const repositoryRoot = fileURLToPath(new URL('../../..', import.meta.url));

/**
 * The bundle the gateway serves to the browser.
 *
 * Passed explicitly rather than left to the launcher's default, which is this
 * same path when it happens to exist (`_web_assets_from_argv`,
 * `src/entrypoints/server.py`). Naming it here is what makes a missing build a
 * named failure instead of a gateway that quietly answers "Web assets are not
 * installed" and a spec that reports whatever the page did next.
 *
 * It is a `vite build` output, not the dev server: nothing about these specs
 * goes through Vite, so editing `clients/web/src` has no effect on what the
 * browser runs until this is rebuilt. `pnpm --dir clients/web test:e2e` builds
 * it first for exactly that reason. A run that skipped the build once measured a
 * bundle eight hours older than the fix under test, and reported the fixed
 * defect as still present.
 */
const webAssets = join(repositoryRoot, 'clients/web/dist');

export function startLiveGateway(): LiveGateway {
  const runtimeDirectory = mkdtempSync(join(tmpdir(), 'vibesys-web-e2e-'));
  const replayLog = join(runtimeDirectory, 'run-events.jsonl');
  const instancePath = join(runtimeDirectory, 'web-gateway.json');
  copyFileSync(join(repositoryRoot, 'clients/tui/dev/fixtures/framework-events.jsonl'), replayLog);
  if (!existsSync(join(webAssets, 'index.html'))) {
    rmSync(runtimeDirectory, {recursive: true, force: true});
    throw new Error(
      `No web bundle at ${webAssets}: run \`pnpm --dir clients/web build\` (or use the ` +
        '`test:e2e` script, which builds first) before running the browser specs',
    );
  }
  let result: GatewayStop | null = null;
  const stop = (): GatewayStop => {
    result ??= stopAndClean(instancePath, runtimeDirectory);
    return result;
  };
  try {
    execFileSync('uv', startArguments(instancePath, replayLog), {
      cwd: repositoryRoot,
      encoding: 'utf8',
      env: {...process.env, BROWSER: 'true'},
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const record = JSON.parse(readFileSync(instancePath, 'utf8')) as {url: string; pid: number};
    return {url: record.url, pid: record.pid, stop};
  } catch (error) {
    // The start command can exit non-zero after the child has already written
    // its instance record, so stop before deleting the file that names the pid.
    stop();
    throw error;
  }
}

function stopAndClean(instancePath: string, runtimeDirectory: string): GatewayStop {
  const errors: string[] = [];
  let status: number | null = null;
  try {
    status = stopGateway(instancePath);
  } catch (error) {
    errors.push(`stop failed: ${String(error)}`);
  }
  // Removed only when `stop` reported success, which is the contract #1028
  // defines: a non-zero status means a process may still hold files under this
  // directory, and removing files another process holds open is the original
  // defect. Measured: after `stop` returns, the directory still holds
  // `web-gateway.json.lock` and `web-gateway.json.log`, which are exactly what
  // an NFS silly-rename leaves behind. Forward-compatible, because `_run_stop`
  // returns 0 unconditionally today, so this is a no-op until #1028 lands and
  // then becomes correct without another edit. Leaving the directory behind is
  // the right outcome when the gateway is still using it.
  if (status === 0) {
    try {
      // ENOENT is suppressed by `force`, ENOTEMPTY is not. Swallowed so
      // teardown cannot replace a spec's real failure with a filesystem error.
      rmSync(runtimeDirectory, {recursive: true, force: true});
    } catch (error) {
      errors.push(`runtime directory cleanup failed: ${String(error)}`);
    }
  } else {
    errors.push(`runtime directory ${runtimeDirectory} kept: stop reported status ${status}`);
  }
  return {status, errors};
}

function startArguments(instancePath: string, replayLog: string): string[] {
  return [
    'run',
    'python',
    '-m',
    'entrypoints.server',
    '--web',
    '--detach',
    '--web-port',
    '0',
    '--web-instance',
    instancePath,
    '--web-assets',
    webAssets,
    '--web-reopen',
    replayLog,
  ];
}

/**
 * Run one spec against a fresh gateway, then stop it and clean up after it.
 *
 * One teardown path for every spec that needs a real gateway, rather than a
 * `try`/`finally` per file that can differ in exactly the ways that matter here.
 * A spec that needs more than the URL (the pid, or its own assertion on the stop
 * status) calls `startLiveGateway` directly instead.
 *
 * A gateway that will not stop is still a failure, reported with `expect.soft`
 * so it is additional to the spec's own failure rather than in place of it. The
 * teardown errors `stop` collected are annotated for the same reason: a
 * filesystem error during cleanup must not replace the assertion that failed.
 */
export async function withGateway(
  run: (gateway: {readonly url: string}) => Promise<void>,
): Promise<void> {
  const gateway = startLiveGateway();
  try {
    await run(gateway);
  } finally {
    const stopped = gateway.stop();
    expect.soft(stopped.status, 'web stop left the gateway running').toBe(0);
    if (stopped.errors.length > 0) {
      test.info().annotations.push({type: 'teardown', description: stopped.errors.join('; ')});
    }
  }
}

function stopGateway(instancePath: string): number | null {
  return spawnSync(
    'uv',
    ['run', 'python', '-m', 'entrypoints.web', 'stop', '--instance', instancePath],
    {
      cwd: repositoryRoot,
      env: {...process.env, BROWSER: 'true'},
      stdio: 'pipe',
    },
  ).status;
}
