/**
 * Two concurrent desktop windows, each attached to its own run, against the fixture backend built
 * in `two-windows-app.mjs` (a package-local `HostConnection` stand-in, never a real SSH host or
 * `vibesys-helper` subprocess). This is the regression spec for the `RunControl` fix that keyed
 * every binding by `(host, run_id)` instead of one shared `#run` slot
 * (`clients/features/run-control/src/main/run-control.ts`): before that fix, a second window's
 * attach silently retired whatever the first window had bound, and either window's close detached
 * "the" attached run regardless of which page actually closed.
 *
 * Scope note on the campaign dashboard: this spec was written to check that opening "the campaign
 * dev-gated view" in one window leaves the other window's run view alone. At the time, no code
 * path reached that view from the desktop shell at all: the dashboard was wired only into
 * `clients/apps/web`'s dev-only fixture/SSE preview (`import.meta.env.DEV && params.has
 * ('campaign-live')`, see `docs/design/live-campaign-streaming.md`), while `createLiveApp()` — the
 * entry both the browser's live page and `desktop-main.tsx` render unconditionally — showed only
 * `RunPage`.
 *
 * That has since changed: `Live` (`clients/apps/web/src/app/compose.tsx`) now renders a
 * `run`/`campaign` toggle unconditionally, backed by `webSessionCampaignStream` folding the same
 * `session.store` the run page reads — reachable from the desktop shell's production build, not
 * dev-only. This spec still does not exercise it: neither window here clicks `#run-campaign-toggle`
 * or asserts on the campaign view's content, so cross-window isolation of that toggle is untested.
 * Given the window-independence property this spec guards (`RunControl`'s composite binding),
 * the toggle only switches local React state per `Live` instance and reads no shared mutable state,
 * so there is no mechanism for it to leak across windows — but that is reasoning about the code, not
 * a check this spec runs. Left as a follow-up rather than added here under this PR's deadline.
 */
import {fileURLToPath} from 'node:url';
import {type ElectronApplication, _electron as electron, expect, test} from '@playwright/test';
import {electronRuntimeArgs} from './electron-launch-policy.mjs';

const APP = fileURLToPath(new URL('./two-windows-app.mjs', import.meta.url));

type WindowLabel = 'a' | 'b';

/** Matches `RUNS` in `two-windows-app.mjs`: each window's run, and the content unique to it. */
const RUNS: Record<
  WindowLabel,
  {
    readonly host: string;
    readonly instanceId: string;
    readonly runId: string;
    readonly content: string;
    readonly transcriptEntries: number;
  }
> = {
  a: {
    host: 'local',
    instanceId: '00000000000a',
    runId: 'shared-run',
    content: 'local-alpha',
    transcriptEntries: 3,
  },
  b: {
    host: 'ssh:bravo',
    instanceId: '00000000000b',
    runId: 'shared-run',
    content: 'ssh-bravo',
    transcriptEntries: 1,
  },
};

interface RunInspection {
  readonly bodyText: string;
  readonly title: string;
}

interface MainControl {
  attach(label: WindowLabel): Promise<void>;
  windowState(label: WindowLabel): {readonly mode: 'welcome' | 'run'} | null;
  inspectRun(label: WindowLabel): Promise<RunInspection>;
  runPageLoaded(label: WindowLabel): Promise<void>;
  runWebContentsIds(): readonly number[];
  contentsIdFor(label: WindowLabel): number | null;
  closeWindow(label: WindowLabel): void;
  hasContents(id: number): boolean;
  state(): {
    readonly ready: boolean;
    readonly logs: readonly string[];
    readonly fatals: readonly string[];
  };
  channelStates(): Readonly<Record<string, {readonly host: string; readonly aborted: boolean}>>;
}

/**
 * Hang guard for a run page's first load. A passing run never waits on it: the wait ends on the
 * page's load event. It only names the step when a load hangs, inside the test's own 30 s limit.
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

function control(app: ElectronApplication) {
  return {
    attach: (label: WindowLabel): Promise<void> =>
      app.evaluate((_electron, label) => {
        const api = (globalThis as typeof globalThis & {vibesysTwoWindowsE2E?: MainControl})
          .vibesysTwoWindowsE2E;
        if (api === undefined) throw new Error('the two-windows E2E control is unavailable');
        return api.attach(label);
      }, label),
    windowState: (label: WindowLabel): Promise<{readonly mode: 'welcome' | 'run'} | null> =>
      app.evaluate((_electron, label) => {
        const api = (globalThis as typeof globalThis & {vibesysTwoWindowsE2E?: MainControl})
          .vibesysTwoWindowsE2E;
        if (api === undefined) throw new Error('the two-windows E2E control is unavailable');
        return api.windowState(label);
      }, label),
    inspectRun: (label: WindowLabel): Promise<RunInspection> =>
      app.evaluate((_electron, label) => {
        const api = (globalThis as typeof globalThis & {vibesysTwoWindowsE2E?: MainControl})
          .vibesysTwoWindowsE2E;
        if (api === undefined) throw new Error('the two-windows E2E control is unavailable');
        return api.inspectRun(label);
      }, label),
    runPageLoaded: (label: WindowLabel): Promise<void> =>
      app.evaluate((_electron, label) => {
        const api = (globalThis as typeof globalThis & {vibesysTwoWindowsE2E?: MainControl})
          .vibesysTwoWindowsE2E;
        if (api === undefined) throw new Error('the two-windows E2E control is unavailable');
        return api.runPageLoaded(label);
      }, label),
    runWebContentsIds: (): Promise<readonly number[]> =>
      app.evaluate(() => {
        const api = (globalThis as typeof globalThis & {vibesysTwoWindowsE2E?: MainControl})
          .vibesysTwoWindowsE2E;
        if (api === undefined) throw new Error('the two-windows E2E control is unavailable');
        return api.runWebContentsIds();
      }),
    contentsIdFor: (label: WindowLabel): Promise<number | null> =>
      app.evaluate((_electron, label) => {
        const api = (globalThis as typeof globalThis & {vibesysTwoWindowsE2E?: MainControl})
          .vibesysTwoWindowsE2E;
        if (api === undefined) throw new Error('the two-windows E2E control is unavailable');
        return api.contentsIdFor(label);
      }, label),
    closeWindow: (label: WindowLabel): Promise<void> =>
      app.evaluate((_electron, label) => {
        const api = (globalThis as typeof globalThis & {vibesysTwoWindowsE2E?: MainControl})
          .vibesysTwoWindowsE2E;
        if (api === undefined) throw new Error('the two-windows E2E control is unavailable');
        api.closeWindow(label);
      }, label),
    hasContents: (id: number): Promise<boolean> =>
      app.evaluate((_electron, id) => {
        const api = (globalThis as typeof globalThis & {vibesysTwoWindowsE2E?: MainControl})
          .vibesysTwoWindowsE2E;
        if (api === undefined) throw new Error('the two-windows E2E control is unavailable');
        return api.hasContents(id);
      }, id),
    state: (): Promise<{readonly logs: readonly string[]; readonly fatals: readonly string[]}> =>
      app.evaluate(() => {
        const api = (globalThis as typeof globalThis & {vibesysTwoWindowsE2E?: MainControl})
          .vibesysTwoWindowsE2E;
        if (api === undefined) throw new Error('the two-windows E2E control is unavailable');
        return api.state();
      }),
    channelStates: (): Promise<
      Readonly<Record<string, {readonly host: string; readonly aborted: boolean}>>
    > =>
      app.evaluate(() => {
        const api = (globalThis as typeof globalThis & {vibesysTwoWindowsE2E?: MainControl})
          .vibesysTwoWindowsE2E;
        if (api === undefined) throw new Error('the two-windows E2E control is unavailable');
        return api.channelStates();
      }),
  };
}

test('two windows attached to two runs stay independent, and closing one leaves the other bound', async () => {
  const args = [APP, ...electronRuntimeArgs()];
  const electronApp = await bounded('launching Electron', electron.launch({args}));
  const api = control(electronApp);
  try {
    // Playwright's loader releases Electron's substituted `app.whenReady()` only once this
    // module finishes evaluating (see `two-windows-app.mjs`), so the main process's own
    // `RunControl`/`FakeLink` construction is still racing this test's first `evaluate` call;
    // wait it out on a control method that is safe to call immediately.
    await expect
      .poll(() => bounded('reading the initial shell state', api.state()))
      .toMatchObject({ready: true});

    await bounded('attaching window a to the local shared run id', api.attach('a'));
    await bounded('attaching window b to the SSH shared run id', api.attach('b'));

    await expect
      .poll(() => bounded('reading window a mode', api.windowState('a')))
      .toMatchObject({
        mode: 'run',
      });
    await expect
      .poll(() => bounded('reading window b mode', api.windowState('b')))
      .toMatchObject({
        mode: 'run',
      });
    expect(await bounded('reading shell state', api.state())).toMatchObject({fatals: []});

    // Each run page navigates asynchronously (`openRunPage` sets `mode` before its `loadURL`
    // settles), so wait on each page's own load event before reading from them. Polling for the
    // pages' URLs under expect.poll's 5 s deadline failed on a cold Electron start (PR #2885),
    // when the first navigation took longer than that to commit.
    await bounded("loading window a's run page", api.runPageLoaded('a'), LOAD_HANG_GUARD_MS);
    await bounded("loading window b's run page", api.runPageLoaded('b'), LOAD_HANG_GUARD_MS);
    expect(await bounded('listing run pages', api.runWebContentsIds())).toHaveLength(2);
    const idA = await bounded("reading window a's contents id", api.contentsIdFor('a'));
    const idB = await bounded("reading window b's contents id", api.contentsIdFor('b'));
    expect(idA).not.toBeNull();
    expect(idB).not.toBeNull();
    expect(idA).not.toBe(idB);

    // Each window shows only its own run's content: no cross-run bleed while both are live. The
    // transcript arrives over the fixture link's own subscribe/event_batch exchange, so poll
    // rather than reading once.
    await expect
      .poll(() =>
        bounded("inspecting window a's run", api.inspectRun('a')).then(result => result.bodyText),
      )
      .toContain(`${RUNS.a.content}-chunk-${RUNS.a.transcriptEntries - 1}`);
    await expect
      .poll(() =>
        bounded("inspecting window b's run", api.inspectRun('b')).then(result => result.bodyText),
      )
      .toContain(`${RUNS.b.content}-chunk-${RUNS.b.transcriptEntries - 1}`);
    const a = await bounded("inspecting window a's run", api.inspectRun('a'));
    const b = await bounded("inspecting window b's run", api.inspectRun('b'));
    for (let index = 0; index < RUNS.a.transcriptEntries; index += 1) {
      expect(a.bodyText).toContain(`${RUNS.a.content}-chunk-${index}`);
    }
    for (let index = 0; index < RUNS.b.transcriptEntries; index += 1) {
      expect(b.bodyText).toContain(`${RUNS.b.content}-chunk-${index}`);
    }
    expect(a.bodyText).not.toContain(RUNS.b.content);
    expect(b.bodyText).not.toContain(RUNS.a.content);

    // Equal run ids must have dialed distinct host connections. This also proves the run reference
    // retained by AppWindow and admitted from each page's sender still includes its host.
    const beforeClose = await bounded(
      'reading channel states before closing a',
      api.channelStates(),
    );
    expect(beforeClose[RUNS.a.instanceId]).toEqual({host: RUNS.a.host, aborted: false});
    expect(beforeClose[RUNS.b.instanceId]).toEqual({host: RUNS.b.host, aborted: false});

    // The regression this spec guards: closing one window must detach only its own run (keyed by
    // `(host, run_id)`), never the same-id binding on the other host. This exercises AppWindow's
    // retained RunRef, sender admission, and its window-close fencing through the real renderer.
    await bounded('closing window a', api.closeWindow('a'));

    await expect
      .poll(() => bounded("checking window a's run page is gone", api.hasContents(idA ?? -1)))
      .toBe(false);

    // Window a's own channel is told to end; window b's is untouched by a's close.
    await expect
      .poll(async () => {
        const states = await bounded('reading channel states after closing a', api.channelStates());
        return states[RUNS.a.instanceId]?.aborted ?? false;
      })
      .toBe(true);
    const afterClose = await bounded('reading channel states after closing a', api.channelStates());
    expect(afterClose[RUNS.a.instanceId]).toEqual({host: RUNS.a.host, aborted: true});
    expect(afterClose[RUNS.b.instanceId]).toEqual({host: RUNS.b.host, aborted: false});

    const remainingIds = await bounded(
      'listing run pages after closing a',
      api.runWebContentsIds(),
    );
    expect(remainingIds).toEqual([idB]);

    const bAfter = await bounded("inspecting window b's run after closing a", api.inspectRun('b'));
    expect(bAfter.bodyText).toEqual(b.bodyText);
    expect(bAfter.bodyText).not.toContain(RUNS.a.content);
    await expect
      .poll(() => bounded('reading window b mode after closing a', api.windowState('b')))
      .toMatchObject({mode: 'run'});

    expect(await bounded('reading shell state after closing a', api.state())).toMatchObject({
      fatals: [],
    });
  } finally {
    await bounded('closing Electron', electronApp.close());
  }
});
