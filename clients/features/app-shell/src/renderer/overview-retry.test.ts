import {expect, test} from 'bun:test';
import type {WelcomeResult} from '@vibesys/ipc';
import {machineLoads} from '../state/index.js';
import {createSidebarStore, type ShellBridge, type WindowAddress} from './index.js';

/** A window that stays on home: these tests do not navigate. */
const NO_ADDRESS: WindowAddress = {
  screen: () => ({kind: 'runs'}),
  push: () => undefined,
  back: () => undefined,
  onRestore: () => () => undefined,
};

type Overview = Awaited<ReturnType<ShellBridge['overview']>>;

const HEARD: Overview = {
  ok: true,
  value: {listings: [], recent: [], skippedRecent: [], eventCursor: 0},
} as unknown as Overview;
const FAILED: WelcomeResult<never> = {
  ok: false,
  error: 'the main process went away',
  authNeeded: false,
};

/** A bridge whose overview answers from `replies`, in order. */
function world(replies: readonly (() => Promise<Overview>)[]) {
  let asked = 0;
  const retried: string[] = [];
  const never = new Promise<never>(() => undefined);
  const ignore = (): void => undefined;
  const bridge: ShellBridge = {
    overview: () => {
      asked += 1;
      const reply = replies[asked - 1];
      return reply === undefined ? never : reply();
    },
    preferences: () => never,
    archive: () => never,
    openRunWindow: () => never,
    runDigests: () => never,
    projectRuns: () => never,
    cancelProjectRuns: () => never,
    retryListing: host => {
      retried.push(host);
      return Promise.resolve({ok: true, value: null});
    },
    onListing: () => ignore,
    onRecent: () => ignore,
    onPreferences: () => ignore,
  };
  return {bridge, retried, asked: () => asked};
}

async function settle(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
}

for (const [name, reply] of [
  ['answers that it failed', () => Promise.resolve(FAILED)],
  ['rejects', () => Promise.reject(new Error('the main process went away'))],
] as const) {
  test(`an overview that ${name} fails the machines instead of waiting forever`, async () => {
    const {bridge} = world([reply]);
    const {store} = createSidebarStore(bridge, () => () => undefined, NO_ADDRESS);
    await settle();
    expect(store.getState().overview).toMatchObject({kind: 'failed'});
    expect(machineLoads(store.getState(), ['local'])).toEqual([
      {
        kind: 'failed',
        host: 'local',
        step: 'gateway',
        reason: 'the main process went away',
      },
    ]);
  });
}

test('retry asks a failed overview again, and asks the machine for its runs', async () => {
  const {bridge, asked, retried} = world([
    () => Promise.resolve(FAILED),
    () => Promise.resolve(HEARD),
  ]);
  const {store, actions} = createSidebarStore(bridge, () => () => undefined, NO_ADDRESS);
  await settle();
  actions.retryLoad('local');
  expect(store.getState().overview).toEqual({kind: 'waiting'});
  await settle();
  expect(store.getState().overview).toEqual({kind: 'heard'});
  expect(asked()).toBe(2);
  expect(retried).toEqual(['local']);
});

test('retry with the overview heard asks only the machine', async () => {
  const {bridge, asked, retried} = world([() => Promise.resolve(HEARD)]);
  const {actions} = createSidebarStore(bridge, () => () => undefined, NO_ADDRESS);
  await settle();
  actions.retryLoad('ssh:host-a');
  await settle();
  expect(asked()).toBe(1);
  expect(retried).toEqual(['ssh:host-a']);
});
