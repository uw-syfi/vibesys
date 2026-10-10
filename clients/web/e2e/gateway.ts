import {execFileSync, spawn, spawnSync} from 'node:child_process';
import {copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createInterface} from 'node:readline';
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
 * filesystem error during cleanup must not replace the assertion that failed,
 * which is #1028's defect and is how this file's real failures were first
 * masked. This supersedes #1047's inline gate in `live.spec.ts`.
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

/** One real live run used by the adverse-connectivity browser matrix. */
interface MatrixGateway {
  readonly url: string;
  readonly pid: number;
  readonly port: number;
  /** Whether the child process still owns the run. */
  isRunning(): boolean;
  /** End the live run and let the runtime publish its terminal event. */
  stop(): Promise<void>;
  /** Remove the whole gateway process without publishing a terminal event. */
  crash(): Promise<void>;
}

interface MatrixGatewayHarness {
  /** Start a fresh event store under the same durable run identity. */
  start(marker: string, port?: number): Promise<MatrixGateway>;
}

/**
 * Resource contract used by the matrix process owner. A real resource is one
 * detached uv/Python process group; tests use an in-memory implementation to
 * drive every worker-exit path without creating a process.
 */
export interface MatrixGatewayResource {
  /** Gracefully stop the resource and resolve only after its child exits. */
  dispose(): Promise<void>;
  /** Synchronously terminate its whole process group during worker exit. */
  terminateForWorkerExit(): void;
  /** Whether the owner has observed the child exit or it never started. */
  terminationConfirmed(): boolean;
}

export interface MatrixGatewayCleanup {
  readonly errors: readonly string[];
  readonly runtimeDirectoryRemoved: boolean;
  readonly terminationConfirmed: boolean;
}

/**
 * Owns all process groups and the runtime directory for one browser test.
 *
 * `dispose` is the ordinary asynchronous path. `terminateForWorkerExit` is the
 * synchronous last resort used by the worker's process-exit hook. It never
 * removes the directory: without observing each child exit, cleanup must
 * retain files that a reparented process may still hold.
 */
export class MatrixGatewayOwner {
  readonly #resources = new Set<MatrixGatewayResource>();
  readonly #removeRuntimeDirectory: () => void;

  constructor(removeRuntimeDirectory: () => void) {
    this.#removeRuntimeDirectory = removeRuntimeDirectory;
  }

  track<Resource extends MatrixGatewayResource>(resource: Resource): Resource {
    this.#resources.add(resource);
    return resource;
  }

  async dispose(): Promise<MatrixGatewayCleanup> {
    const errors: string[] = [];
    for (const resource of this.#resources) {
      try {
        await resource.dispose();
      } catch (error) {
        errors.push(String(error));
      }
    }
    if ([...this.#resources].some(resource => !resource.terminationConfirmed())) {
      this.terminateForWorkerExit();
      errors.push('runtime directory retained because child termination was not confirmed');
      return {errors, runtimeDirectoryRemoved: false, terminationConfirmed: false};
    }
    try {
      this.#removeRuntimeDirectory();
      return {errors, runtimeDirectoryRemoved: true, terminationConfirmed: true};
    } catch (error) {
      errors.push(`runtime directory cleanup failed: ${String(error)}`);
      return {errors, runtimeDirectoryRemoved: false, terminationConfirmed: true};
    }
  }

  terminateForWorkerExit(): void {
    for (const resource of this.#resources) {
      try {
        resource.terminateForWorkerExit();
      } catch {
        // Process exit cannot wait or report. Continue so one failed signal
        // never prevents the remaining process groups from being terminated.
      }
    }
  }
}

/** One worker-wide registry covers process exit outside Playwright teardown. */
const activeMatrixHarnesses = new Set<OwnedMatrixGatewayHarness>();
process.once('exit', () => {
  for (const harness of activeMatrixHarnesses) harness.terminateForWorkerExit();
});

interface MatrixFixtures {
  readonly matrixGateways: MatrixGatewayHarness;
}

/**
 * Playwright test whose fixture owns every matrix child beyond the test body.
 * A timed-out assertion still enters fixture teardown; if the worker itself
 * exits first, the worker hook and each child's stdin lease take over.
 */
export const matrixTest = test.extend<MatrixFixtures>({
  matrixGateways: async ({browserName: _browserName}, use, testInfo) => {
    const harness = new OwnedMatrixGatewayHarness();
    activeMatrixHarnesses.add(harness);
    try {
      await use(harness);
    } finally {
      const cleanup = await harness.dispose();
      if (cleanup.terminationConfirmed) activeMatrixHarnesses.delete(harness);
      if (cleanup.errors.length > 0) {
        expect.soft(cleanup.errors, 'matrix gateway teardown failures').toEqual([]);
        testInfo.annotations.push({type: 'teardown', description: cleanup.errors.join('; ')});
      }
    }
  },
});

/**
 * Every child uses a distinct store below one `same-run` directory, so the
 * server publishes a stable run id and a fresh store id.
 */
class OwnedMatrixGatewayHarness implements MatrixGatewayHarness {
  readonly #runtimeDirectory = mkdtempSync(join(tmpdir(), 'vibesys-web-matrix-'));
  readonly #owner = new MatrixGatewayOwner(() =>
    rmSync(this.#runtimeDirectory, {recursive: true, force: true}),
  );
  #generation = 0;

  async start(marker: string, port = 0): Promise<MatrixGateway> {
    this.#generation += 1;
    const storeDirectory = join(this.#runtimeDirectory, 'same-run', `store-${this.#generation}`);
    mkdirSync(storeDirectory, {recursive: true});
    const child = this.#owner.track(new MatrixGatewayProcess(storeDirectory, marker, port));
    try {
      await child.start();
      return child;
    } catch (error) {
      await child.dispose();
      throw error;
    }
  }

  dispose(): Promise<MatrixGatewayCleanup> {
    return this.#owner.dispose();
  }

  terminateForWorkerExit(): void {
    this.#owner.terminateForWorkerExit();
  }
}

interface ChildExit {
  readonly code: number | null;
  readonly signal: NodeJS.Signals | null;
  readonly stderr: string;
}

const WEB_URL_PREFIX = 'VibeSys web UI: ';

/** Own one child process and the two explicit ways the matrix may end it. */
class MatrixGatewayProcess implements MatrixGateway, MatrixGatewayResource {
  url = '';
  pid = 0;
  port = 0;

  readonly #storeDirectory: string;
  readonly #marker: string;
  readonly #requestedPort: number;
  #exit: Promise<ChildExit> | null = null;
  #exitResult: ChildExit | null = null;
  #closeOwnerLease: (() => void) | null = null;
  #started = false;
  #running = false;

  constructor(storeDirectory: string, marker: string, port: number) {
    this.#storeDirectory = storeDirectory;
    this.#marker = marker;
    this.#requestedPort = port;
  }

  async start(): Promise<void> {
    if (!existsSync(join(webAssets, 'index.html'))) {
      throw new Error(`No web bundle at ${webAssets}: run \`pnpm --dir clients/web build\``);
    }
    const child = spawn(
      'uv',
      [
        'run',
        'python',
        '-m',
        'tests.e2e.web_matrix_gateway',
        '--store-directory',
        this.#storeDirectory,
        '--web-assets',
        webAssets,
        '--marker',
        this.#marker,
        '--port',
        String(this.#requestedPort),
      ],
      {
        cwd: repositoryRoot,
        detached: true,
        env: {...process.env, BROWSER: 'true'},
        // The open stdin pipe is a worker-liveness lease. If Playwright kills
        // this worker, even with SIGKILL, the Python child observes EOF and
        // shuts its runtime down instead of remaining reparented to PID 1.
        stdio: ['pipe', 'pipe', 'pipe'],
      },
    );
    this.#started = true;
    this.#running = true;
    const stderr: string[] = [];
    child.stderr?.setEncoding('utf8');
    child.stderr?.on('data', chunk => stderr.push(String(chunk)));
    child.on('error', error => stderr.push(String(error)));
    child.stdin?.on('error', error => stderr.push(String(error)));
    this.#closeOwnerLease = () => child.stdin?.end();
    this.#exit = new Promise(resolve => {
      child.once('close', (code, signal) => {
        this.#running = false;
        this.#exitResult = {code, signal, stderr: stderr.join('')};
        resolve(this.#exitResult);
      });
    });
    if (child.pid === undefined) {
      const result = await this.#exited();
      throw new Error(`Matrix gateway failed to spawn: ${result.stderr}`);
    }
    this.pid = child.pid;
    this.url = await this.#readCapabilityUrl(child);
    this.port = Number(new URL(this.url).port);
  }

  isRunning(): boolean {
    if (!this.#running || this.pid === 0) return false;
    try {
      process.kill(this.pid, 0);
      return true;
    } catch {
      return false;
    }
  }

  async stop(): Promise<void> {
    if (!this.#running) return;
    this.#closeOwnerLease?.();
    this.#closeOwnerLease = null;
    const result = await this.#exited();
    if (result.code !== 0) {
      throw new Error(
        `Matrix gateway stop exited ${String(result.code)} (${String(result.signal)}): ${result.stderr}`,
      );
    }
  }

  async crash(): Promise<void> {
    if (!this.#running) return;
    this.#signal('SIGKILL');
    const result = await this.#exited();
    if (result.signal !== 'SIGKILL') {
      throw new Error(
        `Matrix gateway crash exited ${String(result.code)} (${String(result.signal)}): ${result.stderr}`,
      );
    }
  }

  async dispose(): Promise<void> {
    if (this.#running) await this.stop();
  }

  terminationConfirmed(): boolean {
    return !this.#started || this.#exitResult !== null;
  }

  terminateForWorkerExit(): void {
    if (!this.#running) return;
    try {
      this.#signal('SIGKILL');
    } catch {
      // The worker-exit path is synchronous and must continue to every child.
    }
  }

  async #readCapabilityUrl(child: ReturnType<typeof spawn>): Promise<string> {
    if (child.stdout === null) throw new Error('Matrix gateway child has no stdout');
    const lines = createInterface({input: child.stdout});
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (callback: () => void): void => {
        if (settled) return;
        settled = true;
        lines.close();
        callback();
      };
      lines.on('line', line => {
        if (!line.startsWith(WEB_URL_PREFIX)) return;
        finish(() => resolve(line.slice(WEB_URL_PREFIX.length)));
      });
      child.once('error', error => finish(() => reject(error)));
      void this.#exited().then(result => {
        finish(() => {
          reject(
            new Error(
              `Matrix gateway exited before publishing its URL (${String(result.code)}, ${String(result.signal)}): ${result.stderr}`,
            ),
          );
        });
      });
    });
  }

  #signal(signal: NodeJS.Signals): void {
    if (this.pid === 0) return;
    try {
      // `uv run` and its Python child share the detached process group. Signal
      // the group so an abrupt test crash cannot orphan the actual gateway.
      process.kill(-this.pid, signal);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error;
    }
  }

  #exited(): Promise<ChildExit> {
    if (this.#exit === null) throw new Error('Matrix gateway process has not started');
    return this.#exit;
  }
}
