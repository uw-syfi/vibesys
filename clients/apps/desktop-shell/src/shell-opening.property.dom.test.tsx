import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {Screen} from '@vibesys/feature-app-shell/state';
import type {HostKey} from '@vibesys/hosts';
import type {WelcomeBridge, WelcomeResult} from '@vibesys/ipc';
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

/**
 * A launch starts the reads of the screen it opens on and of the rail, and nothing for a screen
 * nobody has opened: each host read is a fresh process, and a cold launch's first answer waits its
 * turn behind all of them. A screen starts its own reads when it is first opened, once, however
 * often the user comes back to it.
 */
const ignore = (): void => undefined;
const REMOTE: HostKey = 'ssh:gpu';

/** The reads that decide what Runs and the rail show: a launch starts these and no other. */
const LAUNCH_READS: readonly string[] = [
  'overview',
  'machines',
  'preferences',
  'projectRuns',
  'runDigests',
];

/**
 * The reads each screen starts when first opened, by bridge method. Usage reads on every visit by
 * design (its figures age, and it names the harnesses), so the reads a visit to it starts are left
 * out of the sequences' accounting.
 */
const OPENS: Readonly<Record<Exclude<Screen['kind'], 'run'>, readonly string[]>> = {
  runs: [],
  usage: [],
  machines: ['machine'],
  // Settings opens on Appearance: its machine sections read a machine's clusters and logins when chosen.
  settings: ['machine'],
  'new-run': [
    'describeMachine',
    'helperFeatures',
    'harnesses',
    'examples',
    'compute',
    'agentLogins',
  ],
};

const VISITS = Object.keys(OPENS) as (keyof typeof OPENS)[];

const REFUSED: WelcomeResult<never> = {
  ok: false,
  error: 'the machine did not answer',
  authNeeded: false,
};

/**
 * The bridge of a window with one saved machine besides This Mac and one run on it: every read
 * answers at once (the three that say what exists with it, the rest with a refusal, which is an
 * answer too), and each call is recorded as the method and its text arguments.
 */
function recordingBridge() {
  /** Each call, with the screen the window was on when it was made. */
  const calls: {readonly call: string; readonly on: string}[] = [];
  const window = {on: 'runs'};
  const ok = <T,>(value: T): WelcomeResult<T> => ({ok: true, value});
  const answers: Readonly<Record<string, unknown>> = {
    overview: ok({
      eventCursor: 1,
      recent: [
        {
          host: REMOTE,
          hostLabel: 'gpu',
          project: '/project',
          runId: 'run-a',
          task: null,
          instanceId: null,
          attachedAt: 1,
        },
      ],
      skippedRecent: [],
      listings: [
        {kind: 'listed', host: REMOTE, live: [], ended: [], unreadable: [], unverified: []},
      ],
    }),
    machines: ok([
      {host: 'local', label: 'This Mac', checkout: '/checkout'},
      {host: REMOTE, label: 'gpu', checkout: '/checkout'},
    ]),
    // New run's helper offers examples, so New run asks for them.
    helperFeatures: ok(['examples']),
    preferences: ok({
      appearance: {mode: 'system', light: 'mist', dark: 'midnight'},
      notifications: {enabled: true},
      archived: [],
    }),
  };
  const bridge = new Proxy(
    {platform: 'darwin'},
    {
      get: (target, name) => {
        if (typeof name !== 'string') return undefined;
        if (name in target) return Reflect.get(target, name);
        // A subscription: nothing is pushed.
        if (name.startsWith('on')) return ignore;
        return (...args: unknown[]) => {
          calls.push({
            call: [name, ...args.filter(arg => typeof arg === 'string')].join(' '),
            on: window.on,
          });
          return Promise.resolve(answers[name] ?? REFUSED);
        };
      },
    },
  ) as unknown as WelcomeBridge;
  return {bridge, calls, window};
}

/** Lets what has arrived reach the stores and the page. */
async function settle(): Promise<void> {
  for (let tick = 0; tick < 8; tick += 1) await act(async () => Promise.resolve());
}

const methodOf = (call: string): string => call.split(' ')[0] ?? call;

/** The shell opened on Runs, then the screens `visits` names selected in order; the reads it started. */
async function startedBy(
  visits: readonly (keyof typeof OPENS)[],
): Promise<readonly {readonly call: string; readonly on: string}[]> {
  const world = recordingBridge();
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
  try {
    await act(async () => root.render(<Shell stores={stores} version="0" />));
    await settle();
    for (const kind of visits) {
      world.window.on = kind;
      await act(async () => stores.sidebar.store.dispatch({type: 'select', screen: {kind}}));
      await settle();
    }
    return world.calls;
  } finally {
    await act(async () => root.unmount());
  }
}

test('a launch on Runs starts the reads of Runs and the rail and no other', async () => {
  const names = (await startedBy([])).map(each => methodOf(each.call));
  expect(names.filter(name => !LAUNCH_READS.includes(name))).toEqual([]);
  // Runs did read: the overview and the machines it lists.
  expect(names).toEqual(expect.arrayContaining(['overview', 'machines']));
});

test('property: a screen starts its reads when first opened, once, whatever the visits', async () => {
  await fc.assert(
    fc.asyncProperty(fc.array(fc.constantFrom(...VISITS), {maxLength: 8}), async visits => {
      const calls = await startedBy(visits);
      const hidden = calls
        .filter(each => each.on !== 'usage' && !LAUNCH_READS.includes(methodOf(each.call)))
        .map(each => each.call);
      // Opened or not, a read is started once: coming back to a screen asks nothing again.
      expect(hidden.length, `reads after ${visits.join(', ')}: ${hidden.join('; ')}`).toBe(
        new Set(hidden).size,
      );
      // Exactly the screens opened start their reads: the methods are those of the opened set.
      const opened = new Set(visits.flatMap(kind => OPENS[kind]));
      expect(new Set(hidden.map(methodOf))).toEqual(opened);
    }),
    {numRuns: 30, seed: 3597},
  );
});
