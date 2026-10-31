import {describe, expect, test} from 'bun:test';
import type {HostListing, RunDigestEntry} from '@vibesys/hosts';
import {liveRows} from '../state/index.js';
import {
  createSidebarStore,
  LIVE_DIGEST_REFRESH_MS,
  type ShellBridge,
  type WindowAddress,
} from './index.js';

/** A window that stays on home: these tests do not navigate. */
const NO_ADDRESS: WindowAddress = {
  screen: () => ({kind: 'runs'}),
  push: () => undefined,
  back: () => undefined,
  onRestore: () => () => undefined,
};

/** A scheduler the test advances by hand: a callback runs only when its time has come. */
class ManualClock {
  #now = 0;
  readonly #pending = new Map<number, {readonly at: number; readonly run: () => void}>();
  #next = 1;

  readonly schedule = (run: () => void, delayMs: number): (() => void) => {
    const id = this.#next++;
    this.#pending.set(id, {at: this.#now + delayMs, run});
    return () => void this.#pending.delete(id);
  };

  get pending(): number {
    return this.#pending.size;
  }

  advance(ms: number): void {
    this.#now += ms;
    for (const [id, timer] of [...this.#pending]) {
      if (timer.at > this.#now) continue;
      this.#pending.delete(id);
      timer.run();
    }
  }
}

const LISTING = (live: boolean): HostListing => ({
  kind: 'listed',
  host: 'local',
  live: live
    ? [
        {
          id: '00000000000a',
          projectRoot: '~/projects/queue-rs',
          runId: 'r1',
          status: 'running',
          phase: 'running',
          paused: null,
          end: null,
          startedAt: 1,
          reason: null,
        },
      ]
    : [],
  ended: [],
  unreadable: [],
  unverified: [],
});

function entry(ideas: number): RunDigestEntry {
  return {
    kind: 'read',
    createdAt: 0,
    ideas,
    adoptedRatio: null,
    bestSoFarRatio: 1.5,
    durationSeconds: 60,
    outcome: null,
    closing: null,
    over: false,
  };
}

/** A bridge whose host lists one live run and whose digest reports one more idea each time. */
function world(listing: HostListing): {readonly bridge: ShellBridge; readonly asked: () => number} {
  let asked = 0;
  const never = new Promise<never>(() => undefined);
  const ignore = (): void => undefined;
  const bridge: ShellBridge = {
    overview: () =>
      Promise.resolve({
        ok: true,
        value: {listings: [listing], recent: [], skippedRecent: [], eventCursor: 0},
      }),
    preferences: () => never,
    archive: () => never,
    openRunWindow: () => never,
    runDigests: () => {
      asked += 1;
      return Promise.resolve({
        ok: true,
        value: {kind: 'digests', runs: new Map([['r1', entry(asked)]])},
      });
    },
    projectRuns: () => never,
    retryListing: () => never,
    onListing: ignore,
    onRecent: ignore,
    onPreferences: ignore,
  };
  return {bridge, asked: () => asked};
}

const settle = (): Promise<void> =>
  new Promise(resolve => queueMicrotask(resolve)).then(() => undefined);

async function drain(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) await settle();
}

describe('a live run on the sidebar', () => {
  test('its digest is read again each interval, and the row shows the latest', async () => {
    const clock = new ManualClock();
    const {bridge, asked} = world(LISTING(true));
    const {store} = createSidebarStore(bridge, clock.schedule, NO_ADDRESS);
    await drain();
    expect(asked()).toBe(1);
    expect(liveRows(store.getState())[0]?.trail).toBe('1 idea · 1.50× best so far · 1m');

    clock.advance(LIVE_DIGEST_REFRESH_MS - 1);
    await drain();
    expect(asked()).toBe(1);
    clock.advance(1);
    await drain();
    expect(asked()).toBe(2);
    expect(liveRows(store.getState())[0]?.trail).toBe('2 ideas · 1.50× best so far · 1m');
    expect(clock.pending).toBe(1);
  });

  test('with no live run nothing is scheduled and nothing is asked', async () => {
    const clock = new ManualClock();
    const {bridge, asked} = world(LISTING(false));
    createSidebarStore(bridge, clock.schedule, NO_ADDRESS);
    await drain();
    expect(clock.pending).toBe(0);
    expect(asked()).toBe(0);
  });
});
