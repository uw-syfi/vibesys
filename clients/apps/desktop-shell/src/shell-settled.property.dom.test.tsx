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
 * Every screen the shell draws from the bridge's replies says when it has read what it shows, as
 * `data-state` on its page, and the rail says it for what decides its mark. A screen added to the
 * shell must be added here (the table is typed over every screen kind but the Run screen, which
 * reads through its own session), and then holds the property below or fails it.
 */
const SCREENS: Readonly<Record<Exclude<Screen['kind'], 'run'>, Screen>> = {
  'new-run': {kind: 'new-run'},
  settings: {kind: 'settings'},
  machines: {kind: 'machines'},
  runs: {kind: 'runs'},
  usage: {kind: 'usage'},
};

const ignore = (): void => undefined;
const REMOTE: HostKey = 'ssh:gpu';

/** A reply the bridge owes: asked, and held until the test lets it arrive. */
interface Owed {
  readonly call: string;
  readonly arrive: () => void;
}

/** What a read the bridge cannot answer from the world below is told: the machine says no. */
const REFUSED: WelcomeResult<never> = {
  ok: false,
  error: 'the machine did not answer',
  authNeeded: false,
};

/**
 * The bridge of a window with one saved machine besides This Mac and one run on it: every request
 * is answered only when the test says so, in any order, and each call is a new reply owed. The
 * three reads that say what exists (the overview, the machines, the preferences) answer with it;
 * every read that follows from it (a project's runs, a machine's facts, the usage) is refused, which
 * is an answer too: the screen shows that problem instead of waiting for it.
 */
function owingBridge() {
  const owed: Owed[] = [];
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
        {
          kind: 'listed',
          host: REMOTE,
          live: [],
          ended: [],
          unreadable: [],
          unverified: [],
        },
      ],
    }),
    machines: ok([
      {host: 'local', label: 'This Mac', checkout: '/checkout'},
      {host: REMOTE, label: 'gpu', checkout: '/checkout'},
    ]),
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
          const reply = Promise.withResolvers<unknown>();
          owed.push({
            call: [name, ...args.filter(arg => typeof arg === 'string')].join(' '),
            arrive: () => reply.resolve(answers[name] ?? REFUSED),
          });
          return reply.promise;
        };
      },
    },
  ) as unknown as WelcomeBridge;
  return {bridge, owed};
}

/** Lets what has arrived reach the stores and the page. */
async function settle(): Promise<void> {
  for (let tick = 0; tick < 8; tick += 1) await act(async () => Promise.resolve());
}

interface Seen {
  readonly state: string | null;
  readonly text: string;
}

/** What `selector`'s element says of itself (`data-state`) and shows (its text). */
function seen(container: HTMLElement, selector: string): Seen | null {
  const element = container.querySelector(selector);
  return element === null
    ? null
    : {state: element.getAttribute('data-state'), text: element.textContent ?? ''};
}

/** What the window said while its replies arrived one by one, and after the last. */
interface Run {
  /** The page each time it said ready, and the replies that had arrived by then. */
  readonly pageReady: {readonly page: Seen; readonly after: readonly string[]}[];
  /** The replies that had arrived, and those still owed, each time the rail said ready. */
  readonly railReady: {readonly after: readonly string[]; readonly owed: readonly string[]}[];
  readonly last: {readonly page: Seen | null; readonly rail: Seen | null};
}

/**
 * Opens the shell on `screen` over a bridge that owes every reply, and lets them arrive one at a
 * time in the order `choices` picks, looking at the page and the rail before each.
 */
async function drive(screen: Screen, choices: readonly number[]): Promise<Run> {
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
  const pageReady: Run['pageReady'][number][] = [];
  const railReady: Run['railReady'][number][] = [];
  const arrived: string[] = [];
  try {
    await act(async () => root.render(<Shell stores={stores} version="0" />));
    await act(async () => stores.sidebar.store.dispatch({type: 'select', screen}));
    for (let step = 0; ; step += 1) {
      await settle();
      const page = seen(container, 'main[data-state]');
      if (page?.state === 'ready') pageReady.push({page, after: [...arrived]});
      if (seen(container, 'nav[data-state]')?.state === 'ready') {
        railReady.push({after: [...arrived], owed: world.owed.map(each => each.call)});
      }
      const next = world.owed.splice(
        (choices[step % choices.length] ?? 0) % Math.max(world.owed.length, 1),
        1,
      )[0];
      if (next === undefined) break;
      arrived.push(next.call);
      await act(async () => next.arrive());
    }
    return {
      pageReady,
      railReady,
      last: {
        page: seen(container, 'main[data-state]'),
        rail: seen(container, 'nav[data-state]'),
      },
    };
  } finally {
    await act(async () => root.unmount());
  }
}

/** The reads the rail's mark decides on, which a rail that says ready has had answered. */
function expectRailRead(railReady: Run['railReady']): void {
  for (const early of railReady) {
    const context = `the rail was ready after ${early.after.join(', ')}`;
    // Usage reads the overview too, so one owed later is not the shell's.
    expect(early.after, context).toContain('overview');
    expect(early.after, context).toContain('machines');
    // Each project's runs and digests the overview led to.
    expect(
      early.owed.filter(call => /^(projectRuns|runDigests)\b/.test(call)),
      context,
    ).toEqual([]);
  }
}

for (const [kind, screen] of Object.entries(SCREENS)) {
  test(`property: ${kind} says ready only when nothing it reads can still change what it shows`, async () => {
    await fc.assert(
      fc.asyncProperty(fc.array(fc.nat(), {minLength: 1, maxLength: 80}), async choices => {
        const run = await drive(screen, choices);
        // Everything arrived: the page is ready, and so is the rail.
        expect(run.last.page?.state, `${kind} after every reply`).toBe('ready');
        expect(run.last.rail?.state).toBe('ready');
        // At rest: what the page showed when it said ready is what it shows with everything read.
        for (const early of run.pageReady) {
          expect(early.page.text, `${kind} ready after ${early.after.join(', ')}`).toBe(
            run.last.page?.text ?? '',
          );
        }
        expectRailRead(run.railReady);
      }),
      {numRuns: 25, seed: 5129},
    );
  });
}
