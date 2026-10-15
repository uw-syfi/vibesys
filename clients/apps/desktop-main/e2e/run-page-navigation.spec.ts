/**
 * Navigating one window's run page from one run to another, against the fixture composition in
 * `run-page-navigation-app.mjs`. This is the regression spec for the single-window navigation leak
 * fixed in `AppWindow.openRunPage` (`clients/apps/desktop-main/src/main/window.ts`): before that
 * fix, opening a second run's page replaced the view and took over as the window's current run
 * without ever detaching the first, so `AppWindowDeps.closed` was never told the first run's page
 * was gone and its binding (in production, an SSH `ConnectionLease` plus its subscriptions) leaked
 * for the life of the window.
 */
import {fileURLToPath} from 'node:url';
import {type ElectronApplication, _electron as electron, expect, test} from '@playwright/test';
import {electronRuntimeArgs} from './electron-launch-policy.mjs';

const APP = fileURLToPath(new URL('./run-page-navigation-app.mjs', import.meta.url));

interface MainControl {
  state(): {
    readonly ready: boolean;
    readonly logs: readonly string[];
    readonly fatals: readonly string[];
  };
  openRun(label: 'alpha' | 'bravo'): void;
  windowMode(): 'welcome' | 'run' | null;
  closedRunIds(): readonly string[];
  runWebContentsIds(): readonly number[];
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

function control(app: ElectronApplication) {
  return {
    state: (): Promise<{
      readonly ready: boolean;
      readonly logs: readonly string[];
      readonly fatals: readonly string[];
    }> =>
      app.evaluate(() => {
        const api = (globalThis as typeof globalThis & {vibesysRunPageNavigationE2E?: MainControl})
          .vibesysRunPageNavigationE2E;
        if (api === undefined)
          throw new Error('the run-page-navigation E2E control is unavailable');
        return api.state();
      }),
    openRun: (label: 'alpha' | 'bravo'): Promise<void> =>
      app.evaluate((_electron, label) => {
        const api = (globalThis as typeof globalThis & {vibesysRunPageNavigationE2E?: MainControl})
          .vibesysRunPageNavigationE2E;
        if (api === undefined)
          throw new Error('the run-page-navigation E2E control is unavailable');
        api.openRun(label);
      }, label),
    windowMode: (): Promise<'welcome' | 'run' | null> =>
      app.evaluate(() => {
        const api = (globalThis as typeof globalThis & {vibesysRunPageNavigationE2E?: MainControl})
          .vibesysRunPageNavigationE2E;
        if (api === undefined)
          throw new Error('the run-page-navigation E2E control is unavailable');
        return api.windowMode();
      }),
    closedRunIds: (): Promise<readonly string[]> =>
      app.evaluate(() => {
        const api = (globalThis as typeof globalThis & {vibesysRunPageNavigationE2E?: MainControl})
          .vibesysRunPageNavigationE2E;
        if (api === undefined)
          throw new Error('the run-page-navigation E2E control is unavailable');
        return api.closedRunIds();
      }),
    runWebContentsIds: (): Promise<readonly number[]> =>
      app.evaluate(() => {
        const api = (globalThis as typeof globalThis & {vibesysRunPageNavigationE2E?: MainControl})
          .vibesysRunPageNavigationE2E;
        if (api === undefined)
          throw new Error('the run-page-navigation E2E control is unavailable');
        return api.runWebContentsIds();
      }),
  };
}

test('opening a second run in the same window detaches the first run before taking it over', async () => {
  const args = [APP, ...electronRuntimeArgs()];
  const electronApp = await bounded('launching Electron', electron.launch({args}));
  const api = control(electronApp);
  try {
    await expect
      .poll(() => bounded('reading the initial shell state', api.state()))
      .toMatchObject({ready: true});

    await bounded('opening run-alpha', api.openRun('alpha'));

    // `openRunPage` navigates its view asynchronously, so wait for the page to exist (and commit
    // its URL) before reading from it.
    await expect
      .poll(() => bounded('listing run pages after opening alpha', api.runWebContentsIds()))
      .toHaveLength(1);
    const idsAfterAlpha = await bounded('listing run pages', api.runWebContentsIds());
    expect(await bounded('reading window mode', api.windowMode())).toBe('run');
    expect(await bounded('reading closed run ids', api.closedRunIds())).toEqual([]);

    // The regression this spec guards: opening run-bravo in the same window, without ever closing
    // run-alpha's page, must still detach run-alpha (report it to `AppWindowDeps.closed`) before
    // run-bravo takes over `#currentRunId`.
    await bounded('opening run-bravo', api.openRun('bravo'));

    await expect
      .poll(() => bounded('listing run pages after opening bravo', api.runWebContentsIds()))
      .toHaveLength(1);
    const idsAfterBravo = await bounded('listing run pages', api.runWebContentsIds());
    expect(idsAfterBravo).not.toEqual(idsAfterAlpha);
    expect(await bounded('reading window mode', api.windowMode())).toBe('run');

    await expect
      .poll(() => bounded('reading closed run ids after opening bravo', api.closedRunIds()))
      .toEqual(['run-alpha']);

    expect(await bounded('reading shell state', api.state())).toMatchObject({fatals: []});
  } finally {
    await bounded('closing Electron', electronApp.close());
  }
});
