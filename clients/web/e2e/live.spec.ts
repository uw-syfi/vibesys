import {execFileSync, spawnSync} from 'node:child_process';
import {copyFileSync, mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {expect, test} from '@playwright/test';

const repositoryRoot = fileURLToPath(new URL('../../..', import.meta.url));

interface LiveGateway {
  readonly instancePath: string;
  readonly runtimeDirectory: string;
  readonly url: string;
}

test('renders a recorded run through the live WebSocket gateway', async ({page}) => {
  const gateway = startGateway();
  try {
    const sockets: string[] = [];
    const pageErrors: string[] = [];
    page.on('websocket', socket => sockets.push(socket.url()));
    page.on('pageerror', error => pageErrors.push(error.message));

    await page.goto(gateway.url);
    await expect(page.getByRole('heading', {name: 'round-2'})).toBeVisible();
    await expect(page.getByText('15 folded events')).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
    // Named explicitly as well as counted: a control channel that reported a
    // spurious outage against the real gateway is the failure this banner would
    // introduce, and the aggregate count alone would not say which banner rose.
    await expect(page.getByTestId('controls-banner')).toHaveCount(0);
    await expect.poll(() => sockets.length).toBe(2);
    expect(
      sockets.every(url => {
        const parsed = new URL(url);
        return (
          parsed.protocol === 'ws:' && parsed.pathname === '/ws' && parsed.searchParams.has('token')
        );
      }),
    ).toBe(true);
    expect(pageErrors).toEqual([]);
    await page.screenshot({path: 'artifacts/web-live.png', fullPage: true});
  } finally {
    // Stop exactly once, on every path, and make the removal conditional on
    // the only signal that says the files are free: exit 0, which means no
    // process this host can observe is using the runtime directory. Keeping
    // the directory when it is still in use is the point. Unlinking a file
    // another process holds open is what fails here: on NFS it leaves a
    // `.nfsXXXX` entry and `rmSync` then throws ENOTEMPTY. `expect.soft`
    // records a bad status without throwing, so a stop failure never masks a
    // real assertion failure from the body above.
    const status = stopGateway(gateway.instancePath);
    expect.soft(status).toBe(0);
    if (status === 0) {
      rmSync(gateway.runtimeDirectory, {recursive: true, force: true});
    }
  }
});

function startGateway(): LiveGateway {
  const runtimeDirectory = mkdtempSync(join(tmpdir(), 'vibesys-web-e2e-'));
  const replayLog = join(runtimeDirectory, 'run-events.jsonl');
  const instancePath = join(runtimeDirectory, 'web-gateway.json');
  copyFileSync(join(repositoryRoot, 'clients/tui/dev/fixtures/framework-events.jsonl'), replayLog);
  try {
    execFileSync(
      'uv',
      [
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
      ],
      {
        cwd: repositoryRoot,
        encoding: 'utf8',
        env: {...process.env, BROWSER: 'true'},
        stdio: ['ignore', 'pipe', 'pipe'],
      },
    );
    const record = JSON.parse(readFileSync(instancePath, 'utf8')) as {url: string};
    return {instancePath, runtimeDirectory, url: record.url};
  } catch (error) {
    // A launch that reports failure can still have left a child holding the
    // runtime files, so stop it before removing anything, and keep the
    // directory on the same condition the test's teardown uses.
    if (stopGateway(instancePath) === 0) {
      rmSync(runtimeDirectory, {recursive: true, force: true});
    }
    throw error;
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
