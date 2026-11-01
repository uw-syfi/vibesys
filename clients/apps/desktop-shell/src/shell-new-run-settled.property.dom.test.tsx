import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {HostKey} from '@vibesys/hosts';
import {REQUIRED_HELPER_FEATURES} from '@vibesys/hosts';
import type {WelcomeBridge, WelcomeEvent} from '@vibesys/ipc';
import {exampleCatalogue} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {createShellStores, Shell} from './shell.js';
import {FakeWindowAddress} from './testing/fake-address.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

const ignore = (): void => undefined;
const REMOTE: HostKey = 'ssh:gpu';

/** A reply the bridge owes: asked, and held until the test lets it arrive. */
interface Owed {
  readonly call: string;
  readonly arrive: () => void;
}

/**
 * The bridge of a window whose user last opened a run on `REMOTE`: every read New run makes is
 * answered only when the test says so, in any order, and each call is a new reply owed (the form
 * asks again of the machine it moves to). Calls other screens make are answered at once.
 */
function owingBridge() {
  const owed: Owed[] = [];
  const answers = {overview: false, machines: false};
  const hold = <T,>(call: string, value: T, arrived?: 'overview' | 'machines') => {
    const reply = Promise.withResolvers<{ok: true; value: T}>();
    owed.push({
      call,
      arrive: () => {
        if (arrived !== undefined) answers[arrived] = true;
        reply.resolve({ok: true, value});
      },
    });
    return reply.promise;
  };
  const ok = <T,>(value: T) => Promise.resolve({ok: true as const, value});
  const opened = [
    {
      host: REMOTE,
      hostLabel: 'gpu',
      project: '/project',
      runId: 'run-a',
      task: null,
      instanceId: null,
      attachedAt: 1,
    },
  ];
  const bridge = {
    overview: () =>
      hold(
        'overview',
        {eventCursor: 1, recent: opened, skippedRecent: [], listings: []},
        'overview',
      ),
    machines: () => hold('machines', [], 'machines'),
    preferences: () =>
      ok({
        appearance: {mode: 'system', light: 'mist', dark: 'midnight'},
        notifications: {enabled: true},
        archived: [],
      }),
    onRecent: ignore,
    onListing: ignore,
    onPreferences: (_listener: (event: WelcomeEvent<never>) => void) => undefined,
    onOpenSettings: (_listener: (event: WelcomeEvent<never>) => void) => undefined,
    projectRuns: () => ok([]),
    runDigests: () => ok({kind: 'unsupported' as const}),
    agentLogins: () => ok({kind: 'unsupported' as const}),
    compute: (host: HostKey) => hold(`compute ${host}`, {kind: 'unsupported' as const}),
    describeMachine: (host: HostKey) => hold(`describe ${host}`, {kind: 'unsupported' as const}),
    helperFeatures: (host: HostKey) =>
      hold(`features ${host}`, [...REQUIRED_HELPER_FEATURES, 'harnesses']),
    harnesses: (host: HostKey) =>
      hold(`harnesses ${host}`, {
        kind: 'listed' as const,
        harnesses: [{provider: 'claude', model: 'claude-a', default: true}],
      }),
    examples: (host: HostKey) =>
      hold(`examples ${host}`, {kind: 'offered' as const, value: exampleCatalogue}),
  } as unknown as WelcomeBridge;
  return {bridge, owed, answers};
}

/** Lets what has arrived reach the stores and the page. */
async function settle(): Promise<void> {
  for (let tick = 0; tick < 8; tick += 1) await act(async () => Promise.resolve());
}

/** What New run says of itself and shows: `data-state` of its page, and the page's text. */
function newRunPage(container: HTMLElement): {state: string | null; text: string} | null {
  const page = container.querySelector('main[data-state]');
  return page === null
    ? null
    : {state: page.getAttribute('data-state'), text: page.textContent ?? ''};
}

test('property: New run says ready only when nothing it reads can still change what it shows', async () => {
  await fc.assert(
    fc.asyncProperty(fc.array(fc.nat(), {minLength: 1, maxLength: 60}), async choices => {
      const world = owingBridge();
      const clock = new FakeClock();
      const stores = createShellStores(
        world.bridge,
        {mode: 'light', onChange: () => {}},
        {setAttribute: () => {}},
        clock.schedule,
        {
          messages: {addEventListener: ignore, removeEventListener: ignore},
          address: new FakeWindowAddress(),
          now: () => 0,
          session: () => {
            throw new Error('No user opened a run');
          },
        },
        {},
      );
      const container = document.createElement('div');
      const root = createRoot(container);
      const readyAt: {text: string; after: string[]}[] = [];
      const arrived: string[] = [];
      try {
        await act(async () => root.render(<Shell stores={stores} version="0" />));
        await act(async () =>
          stores.sidebar.store.dispatch({type: 'select', screen: {kind: 'new-run'}}),
        );
        for (let step = 0; ; step += 1) {
          await settle();
          const page = newRunPage(container);
          expect(page).not.toBeNull();
          if (page?.state === 'ready') {
            // The runs and the machines are read by the shell: New run is not ready before them.
            expect(world.answers).toEqual({overview: true, machines: true});
            readyAt.push({text: page.text, after: [...arrived]});
          }
          const next = world.owed.splice(
            (choices[step % choices.length] ?? 0) % Math.max(world.owed.length, 1),
            1,
          )[0];
          if (next === undefined) break;
          arrived.push(next.call);
          await act(async () => next.arrive());
        }
        const last = newRunPage(container);
        // Everything arrived: the screen is ready, on the machine of the run opened last.
        expect(last?.state).toBe('ready');
        const final = last?.text ?? '';
        // At rest: what it showed when it said ready is what it shows with everything read.
        for (const early of readyAt) {
          expect(early.text, `ready after ${early.after.join(', ')}`).toBe(final);
        }
      } finally {
        await act(async () => root.unmount());
      }
    }),
    {numRuns: 40, seed: 5128},
  );
});
