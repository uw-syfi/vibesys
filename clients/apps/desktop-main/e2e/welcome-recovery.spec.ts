import {fileURLToPath} from 'node:url';
import {type ElectronApplication, _electron as electron, expect, test} from '@playwright/test';
import {electronRuntimeArgs} from './electron-launch-policy.mjs';

const APP = fileURLToPath(new URL('./welcome-recovery-app.mjs', import.meta.url));
const SHELL_URL = 'app://vibesys/shell.html';

interface RecoveryState {
  readonly currentWelcomeId: number | null;
  readonly currentWelcomeUrl: string | null;
  readonly logs: readonly string[];
  readonly fatals: readonly string[];
}

interface ShellInspection {
  readonly underChrome: readonly string[];
  readonly bodyText: string;
  readonly confinement: {
    readonly bridge: string;
    readonly nodeRequire: string;
    readonly nodeProcess: string;
  };
}

/** What a request through the bridge answers: a value, or why the main process refused it. */
type BridgeReply =
  | {readonly ok: true; readonly value: unknown}
  | {readonly ok: false; readonly error: string};

interface MainControl {
  startupPagesLoaded(): Promise<void>;
  state(): RecoveryState;
  crashWelcome(): number;
  hasContents(id: number): boolean;
  inspectWelcome(): Promise<ShellInspection>;
  welcomeOverview(): Promise<BridgeReply>;
  openUnauthorizedWelcome(): Promise<{
    readonly unauthorizedId: number;
    readonly currentId: number | null;
  }>;
  unauthorizedOverview(): Promise<BridgeReply>;
  closeUnauthorizedWelcome(): void;
}

/**
 * Hang guard for the startup page's first load. A passing run never waits on it: the wait ends
 * on the page's load event. It only names the step when a load hangs, inside the test's 30 s.
 */
const LOAD_HANG_GUARD_MS = 25_000;

async function bounded<T>(label: string, operation: Promise<T>, hangGuardMs = 8_000): Promise<T> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const expired = new Promise<never>((_resolve, reject) => {
    timeout = setTimeout(() => reject(new Error(`timed out while ${label}`)), hangGuardMs);
  });
  try {
    return await Promise.race([operation, expired]);
  } finally {
    if (timeout !== undefined) clearTimeout(timeout);
  }
}

/** Call `method` of the fixture's control surface inside the Electron main process. */
function control<K extends keyof MainControl>(
  app: ElectronApplication,
  method: K,
  ...args: Parameters<MainControl[K]>
): Promise<Awaited<ReturnType<MainControl[K]>>> {
  return app.evaluate(
    (_electron, request) => {
      const main = (globalThis as typeof globalThis & {vibesysWelcomeRecoveryE2E?: MainControl})
        .vibesysWelcomeRecoveryE2E;
      if (main === undefined) throw new Error('the shell-recovery E2E control is unavailable');
      const call = main[request.method] as (...values: unknown[]) => unknown;
      return call.apply(main, request.args) as Awaited<ReturnType<MainControl[K]>>;
    },
    {method, args},
  );
}

test('a crashed shell renderer is replaced once, and only its successor is answered', async () => {
  const args = [APP, ...electronRuntimeArgs()];
  const electronApp = await bounded('launching Electron', electron.launch({args}));
  try {
    // Wait on the page's own load event, not on a poll for its URL under a fixed deadline that a
    // cold Electron start can outlast (see `first-load.mjs`).
    await bounded(
      'loading the app shell',
      control(electronApp, 'startupPagesLoaded'),
      LOAD_HANG_GUARD_MS,
    );
    expect(await bounded('reading the initial state', control(electronApp, 'state'))).toMatchObject(
      {currentWelcomeUrl: SHELL_URL, fatals: []},
    );
    await expect
      .poll(() =>
        bounded('inspecting the initial shell document', control(electronApp, 'inspectWelcome')),
      )
      // Nothing is drawn under the window's own controls (macOS's traffic lights).
      .toMatchObject({underChrome: []});

    const before = await bounded('reading the ready state', control(electronApp, 'state'));
    expect(before.currentWelcomeId).not.toBeNull();

    const retiredId = await bounded(
      'crashing the shell renderer',
      control(electronApp, 'crashWelcome'),
    );
    await expect
      .poll(async () => {
        const state = await bounded('reading the recovering state', control(electronApp, 'state'));
        return state.currentWelcomeId !== null && state.currentWelcomeId !== retiredId;
      })
      .toBe(true);
    await expect
      .poll(() => bounded('reading the recovered state', control(electronApp, 'state')))
      .toMatchObject({currentWelcomeUrl: SHELL_URL, fatals: []});
    const recovered = await bounded(
      'reading the stable recovered state',
      control(electronApp, 'state'),
    );
    expect(recovered.logs.filter(line => line.startsWith('recovering the shell'))).toEqual([
      'recovering the shell after render-process-gone',
    ]);
    await expect
      .poll(() =>
        bounded('checking the retired renderer', control(electronApp, 'hasContents', retiredId)),
      )
      .toBe(false);

    const shell = await bounded(
      'inspecting the recovered shell document',
      control(electronApp, 'inspectWelcome'),
    );
    expect(shell.confinement).toEqual({
      bridge: 'object',
      nodeRequire: 'undefined',
      nodeProcess: 'undefined',
    });
    await expect(
      bounded(
        'requesting the overview from the recovered document',
        control(electronApp, 'welcomeOverview'),
      ),
    ).resolves.toMatchObject({ok: true, value: {recent: [], listings: []}});

    // A document of the shell's page that no window owns is refused by the main process.
    const unauthorized = await bounded(
      'opening an unauthorized shell document',
      control(electronApp, 'openUnauthorizedWelcome'),
    );
    expect(unauthorized.currentId).toBe(recovered.currentWelcomeId);
    expect(unauthorized.unauthorizedId).not.toBe(unauthorized.currentId);
    await expect(
      bounded(
        'requesting the overview from the unauthorized document',
        control(electronApp, 'unauthorizedOverview'),
      ),
    ).resolves.toMatchObject({ok: false, error: 'not the welcome view'});
    await bounded(
      'closing the unauthorized shell document',
      control(electronApp, 'closeUnauthorizedWelcome'),
    );
  } finally {
    await bounded('closing Electron', electronApp.close());
  }
});
