import {fileURLToPath} from 'node:url';
import {
  type ElectronApplication,
  _electron as electron,
  expect,
  type Page,
  test,
} from '@playwright/test';
import {electronRuntimeArgs} from './electron-launch-policy.mjs';

const APP = fileURLToPath(new URL('./welcome-recovery-app.mjs', import.meta.url));
const WELCOME_URL = 'app://vibesys/welcome.html';
const RUN_URL = 'app://vibesys/desktop.html';

interface RecoveryState {
  readonly currentWelcomeId: number | null;
  readonly currentWelcomeUrl: string | null;
  readonly runId: number | null;
  readonly runUrl: string | null;
  readonly mode: 'welcome' | 'run';
  readonly logs: readonly string[];
  readonly fatals: readonly string[];
}

interface MainControl {
  state(): RecoveryState;
  crashWelcome(): number;
  hasContents(id: number): boolean;
  openUnauthorizedWelcome(): Promise<{
    readonly unauthorizedId: number;
    readonly currentId: number | null;
  }>;
  closeUnauthorizedWelcome(): void;
}

async function pageAt(app: ElectronApplication, url: string, except?: Page): Promise<Page> {
  const eligible = (page: Page): boolean => page !== except && !page.isClosed();
  const atUrl = async (page: Page): Promise<Page> => {
    await page.waitForURL(url);
    return page;
  };
  const existing = app.context().pages().filter(eligible);
  const created = app.waitForEvent('window', {predicate: eligible}).then(atUrl);
  // Electron exposes a new page target before loadURL necessarily commits. Race every already
  // created target and the next target on navigation rather than discarding an early window event.
  return Promise.any([...existing.map(atUrl), created]);
}

function state(app: ElectronApplication): Promise<RecoveryState> {
  return app.evaluate(() => {
    const control = (globalThis as typeof globalThis & {vibesysWelcomeRecoveryE2E?: MainControl})
      .vibesysWelcomeRecoveryE2E;
    if (control === undefined) throw new Error('the welcome-recovery E2E control is unavailable');
    return control.state();
  });
}

function crashWelcome(app: ElectronApplication): Promise<number> {
  return app.evaluate(() => {
    const control = (globalThis as typeof globalThis & {vibesysWelcomeRecoveryE2E?: MainControl})
      .vibesysWelcomeRecoveryE2E;
    if (control === undefined) throw new Error('the welcome-recovery E2E control is unavailable');
    return control.crashWelcome();
  });
}

function hasContents(app: ElectronApplication, id: number): Promise<boolean> {
  return app.evaluate((_electron, id) => {
    const control = (globalThis as typeof globalThis & {vibesysWelcomeRecoveryE2E?: MainControl})
      .vibesysWelcomeRecoveryE2E;
    if (control === undefined) throw new Error('the welcome-recovery E2E control is unavailable');
    return control.hasContents(id);
  }, id);
}

function openUnauthorizedWelcome(
  app: ElectronApplication,
): Promise<{readonly unauthorizedId: number; readonly currentId: number | null}> {
  return app.evaluate(() => {
    const control = (globalThis as typeof globalThis & {vibesysWelcomeRecoveryE2E?: MainControl})
      .vibesysWelcomeRecoveryE2E;
    if (control === undefined) throw new Error('the welcome-recovery E2E control is unavailable');
    return control.openUnauthorizedWelcome();
  });
}

function closeUnauthorizedWelcome(app: ElectronApplication): Promise<void> {
  return app.evaluate(() => {
    const control = (globalThis as typeof globalThis & {vibesysWelcomeRecoveryE2E?: MainControl})
      .vibesysWelcomeRecoveryE2E;
    if (control === undefined) throw new Error('the welcome-recovery E2E control is unavailable');
    control.closeUnauthorizedWelcome();
  });
}

test('a crashed welcome renderer is replaced once without losing its open run', async () => {
  const args = [
    APP,
    ...(process.platform === 'linux' ? ['--headless'] : []),
    ...electronRuntimeArgs(),
  ];
  const electronApp = await electron.launch({args});
  try {
    const welcome = await pageAt(electronApp, WELCOME_URL);
    const run = await pageAt(electronApp, RUN_URL);
    await expect(welcome.locator('#title-strip')).toBeVisible();

    const before = await state(electronApp);
    expect(before.currentWelcomeUrl).toBe(WELCOME_URL);
    expect(before.runUrl).toBe(RUN_URL);
    expect(before.currentWelcomeId).not.toBeNull();
    expect(before.runId).not.toBeNull();

    const oldClosed = welcome.waitForEvent('close');
    const replacementPage = pageAt(electronApp, WELCOME_URL, welcome);
    const retiredId = await crashWelcome(electronApp);
    const replacement = await replacementPage;
    await oldClosed;

    await expect(replacement.locator('#title-strip')).toBeVisible();
    await expect
      .poll(() => state(electronApp))
      .toMatchObject({
        currentWelcomeUrl: WELCOME_URL,
        runId: before.runId,
        runUrl: RUN_URL,
        mode: 'run',
        fatals: [],
      });
    const recovered = await state(electronApp);
    expect(recovered.currentWelcomeId).not.toBe(retiredId);
    expect(
      electronApp
        .context()
        .pages()
        .filter(page => page.url() === WELCOME_URL && !page.isClosed()),
    ).toHaveLength(1);
    expect(recovered.logs.filter(line => line.startsWith('recovering the welcome view'))).toEqual([
      'recovering the welcome view after render-process-gone',
    ]);
    expect(await hasContents(electronApp, retiredId)).toBe(false);

    const confinement = await replacement.evaluate(() => ({
      bridge: typeof (window as Window & {vibesysWelcome?: unknown}).vibesysWelcome,
      nodeRequire: typeof (globalThis as typeof globalThis & {require?: unknown}).require,
      nodeProcess: typeof (globalThis as typeof globalThis & {process?: unknown}).process,
    }));
    expect(confinement).toEqual({
      bridge: 'object',
      nodeRequire: 'undefined',
      nodeProcess: 'undefined',
    });
    const hosts = await replacement.evaluate(async () => {
      const bridge = (
        window as Window & {
          vibesysWelcome?: {hosts(): Promise<unknown>};
        }
      ).vibesysWelcome;
      if (bridge === undefined) throw new Error('the real welcome preload was not installed');
      return bridge.hosts();
    });
    expect(hosts).toEqual({ok: true, value: []});

    await replacement.getByRole('button', {name: 'This Mac'}).click();
    await expect(replacement.getByRole('heading', {name: 'Recent'})).toBeVisible();
    const back = replacement.getByRole('button', {name: 'Back to run'});
    await expect(back).toBeVisible();
    await back.click();
    await expect
      .poll(() => state(electronApp))
      .toMatchObject({
        mode: 'run',
        runId: before.runId,
        fatals: [],
      });
    expect(run.isClosed()).toBe(false);

    const unauthorizedPage = pageAt(electronApp, WELCOME_URL, replacement);
    const unauthorized = await openUnauthorizedWelcome(electronApp);
    const stale = await unauthorizedPage;
    await expect(stale.getByText('not the welcome view')).toBeVisible();
    expect(unauthorized.currentId).toBe(recovered.currentWelcomeId);
    expect(unauthorized.unauthorizedId).not.toBe(unauthorized.currentId);
    await closeUnauthorizedWelcome(electronApp);
  } finally {
    await electronApp.close();
  }
});
