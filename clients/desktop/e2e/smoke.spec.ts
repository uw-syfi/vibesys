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
  // A repeated quit (a second Cmd+Q) still waits for the home server to stop.
  const closed = app.waitForEvent('close');
  await app
    .evaluate(({app: electron}) => {
      electron.quit();
      electron.quit();
    })
    .catch(() => undefined);
  await closed;
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
