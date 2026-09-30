import {execFileSync, spawnSync} from 'node:child_process';
import {copyFileSync, mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

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

export function startLiveGateway(): LiveGateway {
  const runtimeDirectory = mkdtempSync(join(tmpdir(), 'vibesys-web-e2e-'));
  const replayLog = join(runtimeDirectory, 'run-events.jsonl');
  const instancePath = join(runtimeDirectory, 'web-gateway.json');
  copyFileSync(join(repositoryRoot, 'clients/tui/dev/fixtures/framework-events.jsonl'), replayLog);
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
  try {
    // ENOENT is suppressed by `force`, ENOTEMPTY is not: an NFS silly-rename
    // of a file the gateway still held open leaves the directory non-empty.
    // Root-causing that is #1028; swallowing it here keeps teardown from
    // replacing a spec's real failure with a filesystem error.
    rmSync(runtimeDirectory, {recursive: true, force: true});
  } catch (error) {
    errors.push(`runtime directory cleanup failed: ${String(error)}`);
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
    '--web-reopen',
    replayLog,
  ];
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
