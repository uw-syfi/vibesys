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
  readonly mode: 'welcome' | 'run' | null;
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
}

interface MainControl {
  state(): RecoveryState;
  crashWelcome(): number;
  hasContents(id: number): boolean;
  inspectWelcome(): Promise<WelcomeInspection>;
  welcomeHosts(): Promise<unknown>;
  clickWelcomeButton(label: string): Promise<void>;
  openUnauthorizedWelcome(): Promise<{
    readonly unauthorizedId: number;
    readonly currentId: number | null;
  }>;
  inspectUnauthorizedWelcome(): Promise<WelcomeInspection>;
  closeUnauthorizedWelcome(): void;
}

async function bounded<T>(label: string, operation: Promise<T>): Promise<T> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const expired = new Promise<never>((_resolve, reject) => {
    timeout = setTimeout(() => reject(new Error(`timed out while ${label}`)), 8_000);
  });
  try {
    return await Promise.race([operation, expired]);
  } finally {
    if (timeout !== undefined) clearTimeout(timeout);
  }
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

function welcomeHosts(app: ElectronApplication): Promise<unknown> {
  return app.evaluate(() => {
    const control = (globalThis as typeof globalThis & {vibesysWelcomeRecoveryE2E?: MainControl})
      .vibesysWelcomeRecoveryE2E;
    if (control === undefined) throw new Error('the welcome-recovery E2E control is unavailable');
    return control.welcomeHosts();
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

async function verifyUnauthorizedWelcome(
  electronApp: ElectronApplication,
  currentWelcomeId: number | null,
): Promise<void> {
  const unauthorized = await bounded(
    'opening an unauthorized welcome document',
    openUnauthorizedWelcome(electronApp),
  );
  await expect
    .poll(
      async () =>
        (
          await bounded(
            'inspecting the unauthorized welcome document',
            inspectUnauthorizedWelcome(electronApp),
          )
        ).bodyText,
    )
    .toContain('not the welcome view');
  expect(unauthorized.currentId).toBe(currentWelcomeId);
  expect(unauthorized.unauthorizedId).not.toBe(unauthorized.currentId);
  await bounded('closing the unauthorized welcome document', closeUnauthorizedWelcome(electronApp));
}

test('a crashed welcome renderer is replaced once without losing its open run', async () => {
  const args = [APP, ...electronRuntimeArgs()];
  const electronApp = await bounded('launching Electron', electron.launch({args}));
  try {
    await expect
      .poll(() => bounded('reading the initial shell state', state(electronApp)))
      .toMatchObject({currentWelcomeUrl: WELCOME_URL, runUrl: RUN_URL, fatals: []});
    await expect
      .poll(() => bounded('inspecting the initial welcome document', inspectWelcome(electronApp)))
      .toMatchObject({titleStripVisible: true});

    const before = await bounded('reading the ready shell state', state(electronApp));
    expect(before.currentWelcomeId).not.toBeNull();
    expect(before.runId).not.toBeNull();

    const retiredId = await bounded('crashing the welcome renderer', crashWelcome(electronApp));
    await expect
      .poll(async () => {
        const currentId = (await bounded('reading the recovering shell state', state(electronApp)))
          .currentWelcomeId;
        return currentId !== null && currentId !== retiredId;
      })
      .toBe(true);
    await expect
      .poll(() => bounded('reading the recovered shell state', state(electronApp)))
      .toMatchObject({
        currentWelcomeUrl: WELCOME_URL,
        runId: before.runId,
        runUrl: RUN_URL,
        mode: 'run',
        fatals: [],
      });
    const recovered = await bounded('reading the stable recovered state', state(electronApp));
    expect(recovered.logs.filter(line => line.startsWith('recovering the welcome view'))).toEqual([
      'recovering the welcome view after render-process-gone',
    ]);
    await expect
      .poll(() => bounded('checking the retired renderer', hasContents(electronApp, retiredId)))
      .toBe(false);

    const welcome = await bounded(
      'inspecting the recovered welcome document',
      inspectWelcome(electronApp),
    );
    expect(welcome.confinement).toEqual({
      bridge: 'object',
      nodeRequire: 'undefined',
      nodeProcess: 'undefined',
    });
    await expect(
      bounded('requesting hosts from the recovered document', welcomeHosts(electronApp)),
    ).resolves.toEqual({ok: true, value: []});

    await bounded('opening the local host', clickWelcomeButton(electronApp, 'This Mac'));
    await expect
      .poll(
        async () =>
          (await bounded('inspecting the local host page', inspectWelcome(electronApp))).headings,
      )
      .toContain('Recent');
    await expect
      .poll(
        async () =>
          (await bounded('inspecting local host actions', inspectWelcome(electronApp))).buttons,
      )
      .toContain('Back to run');
    await bounded('returning to the run', clickWelcomeButton(electronApp, 'Back to run'));
    await expect
      .poll(() => bounded('reading the resumed run state', state(electronApp)))
      .toMatchObject({mode: 'run', runId: before.runId, fatals: []});

    await verifyUnauthorizedWelcome(electronApp, recovered.currentWelcomeId);
  } finally {
    await bounded('closing Electron', electronApp.close());
  }
});
