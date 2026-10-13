import {fileURLToPath} from 'node:url';
import {type ElectronApplication, _electron as electron, expect, test} from '@playwright/test';
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

interface WelcomeInspection {
  readonly titleStripVisible: boolean;
  readonly headings: readonly string[];
  readonly buttons: readonly string[];
  readonly bodyText: string;
  readonly confinement: {
    readonly bridge: string;
    readonly nodeRequire: string;
    readonly nodeProcess: string;
  };
  readonly hosts: unknown;
}

interface MainControl {
  state(): RecoveryState;
  crashWelcome(): number;
  hasContents(id: number): boolean;
  inspectWelcome(): Promise<WelcomeInspection>;
  clickWelcomeButton(label: string): Promise<void>;
  openUnauthorizedWelcome(): Promise<{
    readonly unauthorizedId: number;
    readonly currentId: number | null;
  }>;
  inspectUnauthorizedWelcome(): Promise<WelcomeInspection>;
  closeUnauthorizedWelcome(): void;
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

function inspectWelcome(app: ElectronApplication): Promise<WelcomeInspection> {
  return app.evaluate(() => {
    const control = (globalThis as typeof globalThis & {vibesysWelcomeRecoveryE2E?: MainControl})
      .vibesysWelcomeRecoveryE2E;
    if (control === undefined) throw new Error('the welcome-recovery E2E control is unavailable');
    return control.inspectWelcome();
  });
}

function clickWelcomeButton(app: ElectronApplication, label: string): Promise<void> {
  return app.evaluate((_electron, label) => {
    const control = (globalThis as typeof globalThis & {vibesysWelcomeRecoveryE2E?: MainControl})
      .vibesysWelcomeRecoveryE2E;
    if (control === undefined) throw new Error('the welcome-recovery E2E control is unavailable');
    return control.clickWelcomeButton(label);
  }, label);
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

function inspectUnauthorizedWelcome(app: ElectronApplication): Promise<WelcomeInspection> {
  return app.evaluate(() => {
    const control = (globalThis as typeof globalThis & {vibesysWelcomeRecoveryE2E?: MainControl})
      .vibesysWelcomeRecoveryE2E;
    if (control === undefined) throw new Error('the welcome-recovery E2E control is unavailable');
    return control.inspectUnauthorizedWelcome();
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
    await expect
      .poll(() => state(electronApp))
      .toMatchObject({currentWelcomeUrl: WELCOME_URL, runUrl: RUN_URL, fatals: []});
    await expect.poll(() => inspectWelcome(electronApp)).toMatchObject({titleStripVisible: true});

    const before = await state(electronApp);
    expect(before.currentWelcomeId).not.toBeNull();
    expect(before.runId).not.toBeNull();

    const retiredId = await crashWelcome(electronApp);
    await expect
      .poll(async () => {
        const currentId = (await state(electronApp)).currentWelcomeId;
        return currentId !== null && currentId !== retiredId;
      })
      .toBe(true);
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
    expect(recovered.logs.filter(line => line.startsWith('recovering the welcome view'))).toEqual([
      'recovering the welcome view after render-process-gone',
    ]);
    await expect.poll(() => hasContents(electronApp, retiredId)).toBe(false);

    const welcome = await inspectWelcome(electronApp);
    expect(welcome.confinement).toEqual({
      bridge: 'object',
      nodeRequire: 'undefined',
      nodeProcess: 'undefined',
    });
    expect(welcome.hosts).toEqual({ok: true, value: []});

    await clickWelcomeButton(electronApp, 'This Mac');
    await expect.poll(async () => (await inspectWelcome(electronApp)).headings).toContain('Recent');
    await expect
      .poll(async () => (await inspectWelcome(electronApp)).buttons)
      .toContain('Back to run');
    await clickWelcomeButton(electronApp, 'Back to run');
    await expect
      .poll(() => state(electronApp))
      .toMatchObject({mode: 'run', runId: before.runId, fatals: []});

    const unauthorized = await openUnauthorizedWelcome(electronApp);
    await expect
      .poll(async () => (await inspectUnauthorizedWelcome(electronApp)).bodyText)
      .toContain('not the welcome view');
    expect(unauthorized.currentId).toBe(recovered.currentWelcomeId);
    expect(unauthorized.unauthorizedId).not.toBe(unauthorized.currentId);
    await closeUnauthorizedWelcome(electronApp);
  } finally {
    await electronApp.close();
  }
});
