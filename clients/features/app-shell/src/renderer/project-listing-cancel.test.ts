import {describe, expect, test} from 'bun:test';
import type {HostListing, ListedRun} from '@vibesys/hosts';
import type {WelcomeResult} from '@vibesys/ipc';
import {createSidebarStore, type ShellBridge} from './index.js';

const LISTING: HostListing = {
  kind: 'listed',
  host: 'local',
  live: [
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
  ],
  ended: [],
  unreadable: [],
  unverified: [],
};

/**
 * A host whose project listings stay open until the test answers or cancels them, as the main
 * process would: each listing is named by the token the view sent.
 */
function world() {
  const never = new Promise<never>(() => undefined);
  const ignore = (): void => undefined;
  const open = new Map<number, (result: WelcomeResult<readonly ListedRun[]>) => void>();
  const asked: number[] = [];
  const cancelled: number[] = [];
  const bridge: ShellBridge = {
    overview: () =>
      Promise.resolve({
        ok: true,
        value: {listings: [LISTING], recent: [], skippedRecent: [], eventCursor: 0},
      }),
    preferences: () => never,
    archive: () => never,
    openRunWindow: () => never,
    runDigests: () => never,
    retryListing: () => never,
    onListing: ignore,
    onRecent: ignore,
    onPreferences: ignore,
    projectRuns: (_host, _project, token) =>
      new Promise(resolve => {
        asked.push(token);
        open.set(token, resolve);
      }),
    cancelProjectRuns: token => {
      cancelled.push(token);
      open.get(token)?.({ok: false, error: 'the project listing was cancelled', authNeeded: false});
      return Promise.resolve({ok: true, value: null});
    },
  };
  return {bridge, asked, cancelled, open};
}

const settle = async (): Promise<void> => {
  for (let turn = 0; turn < 8; turn += 1)
    await new Promise(resolve => queueMicrotask(() => resolve(0)));
};

describe('a project listing the Runs screen no longer waits for', () => {
  test('is cancelled by the token it was sent under when the screen asks again', async () => {
    const {bridge, asked, cancelled} = world();
    const {store} = createSidebarStore(bridge, () => () => undefined);
    await settle();
    expect(asked).toHaveLength(1);
    expect(cancelled).toEqual([]);

    store.dispatch({type: 'select', screen: {kind: 'new-run'}});
    store.dispatch({type: 'select', screen: {kind: 'runs'}});
    await settle();
    // The first listing is still being read: it is cancelled by its own token, and asked again.
    expect(cancelled).toEqual([asked[0] as number]);
    expect(asked).toHaveLength(2);
    expect(asked[1]).not.toBe(asked[0]);

    // Staying and leaving cancels nothing more.
    store.dispatch({type: 'select', screen: {kind: 'new-run'}});
    await settle();
    expect(cancelled).toHaveLength(1);
  });

  test('a listing that answered is not cancelled, and its slot is not left loading', async () => {
    const {bridge, asked, cancelled, open} = world();
    const {store} = createSidebarStore(bridge, () => () => undefined);
    await settle();
    open.get(asked[0] as number)?.({ok: true, value: []});
    await settle();
    store.dispatch({type: 'select', screen: {kind: 'new-run'}});
    store.dispatch({type: 'select', screen: {kind: 'runs'}});
    await settle();
    expect(cancelled).toEqual([]);
    expect(Object.values(store.getState().allRuns.projects).map(slot => slot.kind)).toEqual([
      'loading',
    ]);
    expect(asked).toHaveLength(2);
  });

  test('a failure within the bound frees the slot: it is failed, not loading', async () => {
    const {bridge, asked, open} = world();
    const {store} = createSidebarStore(bridge, () => () => undefined);
    await settle();
    open.get(asked[0] as number)?.({
      ok: false,
      error: 'gRPC deadline exceeded: no frame for 30s',
      authNeeded: false,
    });
    await settle();
    expect(Object.values(store.getState().allRuns.projects).map(slot => slot.kind)).toEqual([
      'failed',
    ]);
  });
});
