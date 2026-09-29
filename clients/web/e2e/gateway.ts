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
 */
export interface LiveGateway {
  readonly url: string;
  stop(): void;
}

const repositoryRoot = fileURLToPath(new URL('../../..', import.meta.url));

export function startLiveGateway(): LiveGateway {
  const runtimeDirectory = mkdtempSync(join(tmpdir(), 'vibesys-web-e2e-'));
  const replayLog = join(runtimeDirectory, 'run-events.jsonl');
  const instancePath = join(runtimeDirectory, 'web-gateway.json');
  copyFileSync(join(repositoryRoot, 'clients/tui/dev/fixtures/framework-events.jsonl'), replayLog);
  let stopped = false;
  const stop = (): void => {
    if (stopped) return;
    stopped = true;
    stopGateway(instancePath);
    rmSync(runtimeDirectory, {recursive: true, force: true});
  };
  try {
    execFileSync('uv', startArguments(instancePath, replayLog), {
      cwd: repositoryRoot,
      encoding: 'utf8',
      env: {...process.env, BROWSER: 'true'},
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const record = JSON.parse(readFileSync(instancePath, 'utf8')) as {url: string};
    return {url: record.url, stop};
  } catch (error) {
    rmSync(runtimeDirectory, {recursive: true, force: true});
    throw error;
  }
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
