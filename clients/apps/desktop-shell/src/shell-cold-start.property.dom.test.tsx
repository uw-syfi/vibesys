import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {HostRun} from '@vibesys/hosts';
import type {PreferencesView, WelcomeBridge, WelcomeEvent} from '@vibesys/ipc';
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
const STORED: PreferencesView = {
  appearance: {mode: 'system', light: 'mist', dark: 'midnight'},
  notifications: {enabled: true},
  archived: [],
};
/** How long the shell waits for its machines before it stops waiting, in milliseconds. */
const WAIT_MS = 4000;

const ARRIVALS = ['overview', 'recent', 'listing', 'preferences', 'machines'] as const;
type Arrival = (typeof ARRIVALS)[number];
const SCENARIO = fc.record({
  holdsRuns: fc.boolean(),
  order: fc.shuffledSubarray([...ARRIVALS], {minLength: 5, maxLength: 5}),
  // Time between arrivals; a long one lets the wait for the machines run out first.
  gaps: fc.array(fc.constantFrom(1, 50, 1500, 3999, 4001, 9000), {minLength: 5, maxLength: 5}),
});

/** Lets the replies of a bridge that answers at once reach the stores and the page. */
async function settle(): Promise<void> {
  for (let tick = 0; tick < 8; tick += 1) await act(async () => Promise.resolve());
}

/** The machines' replies, held until the test lets each one arrive. */
function coldStartWorld(holdsRuns: boolean) {
  const overview = Promise.withResolvers<Awaited<ReturnType<WelcomeBridge['overview']>>>();
  const preferences = Promise.withResolvers<Awaited<ReturnType<WelcomeBridge['preferences']>>>();
  const machines = Promise.withResolvers<Awaited<ReturnType<WelcomeBridge['machines']>>>();
  let recent: Parameters<WelcomeBridge['onRecent']>[0] = () => {};
  let listing: Parameters<WelcomeBridge['onListing']>[0] = () => {};
  const ok = <T,>(value: T) => Promise.resolve({ok: true as const, value});
  const opened = holdsRuns
    ? [
        {
          host: 'local' as const,
          hostLabel: 'Local',
          project: '/project',
          runId: 'run-a',
          task: null,
          instanceId: null,
          attachedAt: 1,
        },
      ]
    : [];
  const live: HostRun[] = holdsRuns
    ? [
        {
          id: 'a',
          projectRoot: '/project',
          runId: 'run-a',
          status: 'running',
          phase: 'running',
          paused: null,
          end: null,
          startedAt: 1,
          reason: null,
        },
      ]
    : [];
  const listed: Parameters<Parameters<WelcomeBridge['onListing']>[0]>[0]['value'] = {
    kind: 'listed' as const,
    host: 'local' as const,
    unreadable: [],
    unverified: [],
    live,
    ended: [],
  };
  const bridge = {
    overview: () => overview.promise,
    preferences: () => preferences.promise,
    machines: () => machines.promise,
    onRecent: (receive: typeof recent) => {
      recent = receive;
    },
    onListing: (receive: typeof listing) => {
      listing = receive;
    },
    onPreferences: (_listener: (event: WelcomeEvent<never>) => void) => undefined,
    onOpenSettings: (_listener: (event: WelcomeEvent<never>) => void) => undefined,
    projectRuns: () => ok([]),
    runDigests: () => ok({kind: 'unsupported' as const}),
    describeMachine: () => ok({kind: 'unsupported' as const}),
    harnesses: () => ok({kind: 'unsupported' as const}),
    examples: () => ok({kind: 'unsupported' as const}),
    compute: () => ok({kind: 'unsupported' as const}),
    agentLogins: () => ok({kind: 'unsupported' as const}),
  } as unknown as WelcomeBridge;
  const arrivals: Record<Arrival, () => void> = {
    overview: () =>
      overview.resolve({
        ok: true,
        value: {eventCursor: 1, recent: opened, skippedRecent: [], listings: [listed]},
      }),
    recent: () => recent({sequence: 2, value: opened}),
    listing: () => listing({sequence: 3, value: listed}),
    preferences: () => preferences.resolve({ok: true, value: STORED}),
    machines: () => machines.resolve({ok: true, value: []}),
  };
  return {bridge, arrive: (event: Arrival) => arrivals[event]()};
}

function coldStartStores(bridge: WelcomeBridge, clock: FakeClock) {
  return createShellStores(
    bridge,
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
}

/** What the Runs screen says: it is reading, it holds a run, or it says there is none. */
function runsScreenSays(container: HTMLElement): 'reading' | 'runs' | 'none' | null {
  if (container.querySelector('[role="progressbar"]') !== null) return 'reading';
  if (container.textContent?.includes('No runs')) return 'none';
  return container.querySelector('.vs-overview-tile') === null ? null : 'runs';
}

test('property: a cold start with any arrival order and timing never leaves Runs by itself', async () => {
  await fc.assert(
    fc.asyncProperty(SCENARIO, async ({holdsRuns, order, gaps}) => {
      const world = coldStartWorld(holdsRuns);
      const clock = new FakeClock();
      const stores = coldStartStores(world.bridge, clock);
      const container = document.createElement('div');
      const root = createRoot(container);
      const arrived = new Set<Arrival>();
      // No one pressed anything: the screen is the one the window opened on, at every instant.
      const check = () => {
        expect(stores.sidebar.store.getState().selection.kind).toBe('runs');
        const says = runsScreenSays(container);
        // Listing events wait for the overview that carries their cursor: only it tells the shell of runs.
        const settledEmpty = !holdsRuns && arrived.has('overview') && arrived.has('machines');
        if (settledEmpty) expect(says).toBe('none');
        else if (!arrived.has('overview')) expect(says).toBe('reading');
        else if (holdsRuns) expect(says).not.toBe('none');
      };
      try {
        await act(async () => root.render(<Shell stores={stores} version="0" />));
        check();
        for (const [index, event] of order.entries()) {
          await act(async () => clock.advanceBy(gaps[index] ?? 1));
          await settle();
          check();
          await act(async () => world.arrive(event));
          arrived.add(event);
          await settle();
          check();
        }
        await act(async () => clock.advanceBy(WAIT_MS + 1));
        await settle();
        check();
      } finally {
        await act(async () => root.unmount());
      }
    }),
    {numRuns: 20, seed: 341813},
  );
});

test('a listing that arrives long after the old four second bound finds the window still on Runs', async () => {
  const world = coldStartWorld(true);
  const clock = new FakeClock();
  const stores = coldStartStores(world.bridge, clock);
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () => root.render(<Shell stores={stores} version="0" />));
    await act(async () => clock.advanceBy(WAIT_MS * 10));
    await settle();
    expect(stores.sidebar.store.getState().selection.kind).toBe('runs');
    expect(runsScreenSays(container)).toBe('reading');
    for (const event of ARRIVALS) await act(async () => world.arrive(event));
    await settle();
    expect(stores.sidebar.store.getState().selection.kind).toBe('runs');
    expect(runsScreenSays(container)).toBe('runs');
  } finally {
    await act(async () => root.unmount());
  }
});

test('the empty Runs screen offers New run, and pressing it is what opens New run', async () => {
  const world = coldStartWorld(false);
  const clock = new FakeClock();
  const stores = coldStartStores(world.bridge, clock);
  const container = document.createElement('div');
  const root = createRoot(container);
  try {
    await act(async () => root.render(<Shell stores={stores} version="0" />));
    for (const event of ARRIVALS) await act(async () => world.arrive(event));
    await settle();
    expect(runsScreenSays(container)).toBe('none');
    const press = [...container.querySelectorAll('button')].find(
      each => each.textContent === 'New run',
    );
    expect(press).toBeDefined();
    await act(async () => press?.click());
    expect(stores.sidebar.store.getState().selection.kind).toBe('new-run');
  } finally {
    await act(async () => root.unmount());
  }
});
