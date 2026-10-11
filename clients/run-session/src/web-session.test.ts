import {describe, expect, test} from 'bun:test';
import type {ProtocolResponse} from '@vibesys/contracts';
import {event, eventBatch, snapshotResponse} from '@vibesys/contracts/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {
  deferred,
  disconnect,
  FakeLifecycle,
  healthy,
  lost,
  outputBatch,
  sessionWith,
  streamBatch,
  waitForSession,
} from './testing/index.js';

describe('WebSession', () => {
  test('wakes after an OS sleep with an exhausted schedule and catches up exactly', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [],
      scheduleTimeout: scheduler.scheduleTimeout,
    });

    await session.start();
    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState().sequence).toBe(1);
    expect(transport.subscriptions[0]).toMatchObject({afterSequence: 0});

    lifecycle.setVisibility('hidden');
    lifecycle.setOnline(false);
    expect(session.getState().status).toBe('stale');
    transport.dropControlChannel(disconnect('laptop slept'));
    transport.subscriptions[0]?.onDisconnect(disconnect('laptop slept'));
    expect(scheduler.pendingDelays()).toEqual([]);

    // Network returns while the page is still suspended. The lifecycle policy
    // waits for visibility rather than claiming the hidden page can make
    // progress, and the empty schedule represents timers exhausted during the
    // wall-clock gap.
    lifecycle.setOnline(true);
    expect(transport.subscriptions).toHaveLength(1);

    transport.silentDials = 1;
    const resumed = transport.nextSubscription();
    const recovered = waitForSession(session, () => {
      return (
        session.getState().status === 'connected' &&
        session.getState().controls.status === 'connected' &&
        session.store.getState().sequence === 2
      );
    });
    lifecycle.setVisibility('visible');
    expect(transport.reconnectCalls).toBe(1);
    const subscription = await resumed;
    expect(subscription).toMatchObject({
      afterSequence: 1,
      options: {storeId: 'store-1'},
    });
    subscription.onMessage(outputBatch('store-1', 2, 'caught up after sleep\n'));
    await recovered;
    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState().sequence).toBe(2);
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'caught up after sleep\n',
    ]);

    await session.close();
    expect(transport.closeCalls).toBe(1);
    expect(lifecycle.listenerCount('online')).toBe(0);
    lifecycle.setOnline(false);
    expect(transport.subscriptions).toHaveLength(2);
  });

  test('wakes a throttled tab without waiting for its skipped reconnect timer', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [50],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    await session.start();

    lifecycle.setVisibility('hidden');
    transport.dropControlChannel(disconnect('backgrounded'));
    transport.subscriptions[0]?.onDisconnect(disconnect('backgrounded'));
    expect(scheduler.pendingDelays()).toEqual([50]);

    transport.silentDials = 1;
    const resumed = transport.nextSubscription();
    const recovered = waitForSession(session, () => {
      return session.getState().status === 'connected' && session.store.getState().sequence === 2;
    });
    lifecycle.setVisibility('visible');
    expect(transport.reconnectCalls).toBe(1);
    const subscription = await resumed;

    expect(scheduler.pendingDelays()).toEqual([]);
    expect(subscription).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});
    subscription.onMessage(outputBatch('store-1', 2, 'caught up after throttling\n'));
    await recovered;

    // The wake retired the skipped reconnect timer: nothing is left to fire,
    // so no second subscription opens.
    expect(scheduler.runNext()).toBe(false);
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'caught up after throttling\n',
    ]);
    await session.close();
  });

  test('releases recovery ownership when control reconnect throws synchronously', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    await session.start();

    const failure = new Error('control reconnect failed');
    transport.reconnectFailures.push(failure);
    lifecycle.emit('online');
    expect(session.getState()).toMatchObject({status: 'stale', error: failure});

    const recovered = transport.nextSubscription();
    session.reattach();
    expect(transport.reconnectCalls).toBe(2);
    await expect(recovered).resolves.toMatchObject({afterSequence: 0, options: undefined});
    await session.close();
  });

  test('regression: a late start-up snapshot never overwrites a newer refresh snapshot', async () => {
    // The shell used to fold each loaded snapshot before the core checked its reply, so the
    // start-up snapshot, answered after an overlapping refresh's, rolled the run's status back.
    // Both are at one sequence, where the run fold itself cannot tell which is newer.
    const older = snapshotResponse({sequence: 0, status: 'running'});
    const newer = snapshotResponse({sequence: 0, status: 'paused'});
    for (const startupFirst of [false, true]) {
      const lifecycle = new FakeLifecycle();
      const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
      const startup = deferred<ProtocolResponse>();
      const refresh = deferred<ProtocolResponse>();
      transport.snapshots.push(startup.promise, refresh.promise);
      // The stream subscribes without a bootstrap batch, so only the snapshots move the fold.
      transport.silentDials = 1;
      const started = session.start();
      lifecycle.emit('online');
      expect(transport.requests).toHaveLength(2);

      const status = () => session.store.getState().status;
      const answerStartup = async () => {
        startup.resolve(older);
        await started;
      };
      const answerRefresh = async () => {
        refresh.resolve(newer);
        await waitForSession(session, () => status() === 'paused');
      };
      if (startupFirst) {
        await answerStartup();
        await answerRefresh();
      } else {
        await answerRefresh();
        await answerStartup();
      }
      expect({startupFirst, status: status()}).toEqual({
        startupFirst,
        status: 'paused',
      });
      await session.close();
    }
  });

  test('promotes an overlapping lifecycle retry to an explicit reattach', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    await session.start();
    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    expect(session.getState().status).toBe('stale');

    const snapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(snapshot.promise);
    lifecycle.emit('online');
    expect(transport.requests).toHaveLength(2);
    expect(transport.reconnectCalls).toBe(1);

    // The lifecycle retry owns the one control redial and snapshot request,
    // but an explicit user request that arrives before that snapshot settles
    // must promote its eventual stream recovery to a fresh bootstrap.
    session.reattach();
    session.reattach();
    lifecycle.emit('online');
    expect(transport.requests).toHaveLength(2);
    expect(transport.reconnectCalls).toBe(1);
    expect(transport.subscriptions).toHaveLength(1);

    const recovered = transport.nextSubscription();
    snapshot.resolve(snapshotResponse({sequence: 1}));
    expect(await recovered).toMatchObject({afterSequence: 0, options: undefined});
    expect(transport.subscriptions).toHaveLength(2);

    await session.close();
  });

  test('keeps an explicit reattach behind a lifecycle stream dial', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    await session.start();

    transport.silentDials = 1;
    transport.deferNextSubscribe();
    const retrying = transport.nextSubscription();
    const retiring = transport.nextSubscriptionClose();
    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    lifecycle.emit('online');
    await retiring;

    // The retry has taken ownership and begun retiring the stale handle, but
    // the session's flight must already be reusable when its public caller can
    // observe that transition. Otherwise this stronger intent is dropped in a
    // Promise.finally tail after the retry has already been dispatched.
    transport.snapshots.push(new Error('reattach snapshot failed'));
    const refreshed = waitForSession(
      session,
      () => session.getState().error?.message === 'reattach snapshot failed',
    );
    session.reattach();
    session.reattach();
    expect(transport.requests).toHaveLength(3);
    await refreshed;

    const resumed = await retrying;
    expect(resumed).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});

    resumed.onMessage(outputBatch('store-1', 2, 'superseded retry\n'));
    expect(session.store.getState().sequence).toBe(1);
    expect(session.store.getState().transcript).toEqual([]);

    const fresh = transport.nextSubscription();
    transport.releasePendingSubscribe();
    await expect(fresh).resolves.toMatchObject({afterSequence: 0, options: undefined});

    expect(
      transport.subscriptions.slice(1).map(({afterSequence, options}) => ({
        afterSequence,
        options,
      })),
    ).toEqual([
      {afterSequence: 1, options: {storeId: 'store-1'}},
      {afterSequence: 0, options: undefined},
    ]);
    expect(resumed.closed).toBe(true);
    await session.close();
  });

  test('coalesces reattach with a recovery bootstrap before the first batch', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.silentDials = 2;
    await session.start();

    transport.deferNextSubscribe();
    const retrying = transport.nextSubscription();
    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    expect(scheduler.runNext()).toBe(true);
    expect(await retrying).toMatchObject({afterSequence: 0, options: undefined});

    session.reattach();
    session.reattach();
    expect(transport.subscriptions).toHaveLength(2);

    const recovered = waitForSession(session, () => session.getState().status === 'connected');
    transport.releasePendingSubscribe();
    await recovered;

    expect(transport.subscriptions).toHaveLength(2);
    await session.close();
  });

  test('coalesces reattach with a pending initial stream bootstrap', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.deferNextSubscribe();
    const initialSubscription = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialSubscription;

    // start() has completed its snapshot but still owns the first sequence-zero
    // handshake. Reattach is the same fresh intent, so it must not create a
    // second socket whose handle could later be orphaned by the first one.
    session.reattach();
    session.reattach();
    expect(transport.subscriptions).toHaveLength(1);

    transport.releasePendingSubscribe();
    await starting;
    await session.close();
    expect(initial.closed).toBe(true);
    expect(transport.subscriptions.every(subscription => subscription.closed)).toBe(true);
  });

  test('re-bootstraps when one store raises its declared history floor', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();

    transport.subscriptions[0]?.onMessage(streamBatch('run-store', 100, 10));
    transport.subscriptions[0]?.onMessage(streamBatch('run-store', 20, 50));

    expect(session.store.getState().sequence).toBe(20);
    expect(session.store.getState().historyAfterSequence).toBe(50);
    await session.close();
  });

  test('uses the fresh-path empty-store rule instead of keeping a stale identity', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();

    transport.subscriptions[0]?.onMessage(streamBatch('run-store', 100));
    transport.subscriptions[0]?.onMessage(streamBatch('', 2));

    expect(session.store.getState().sequence).toBe(2);
    await session.close();
  });

  test('replaces the fold when a store swap reboots mid-subscription', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();

    transport.subscriptions[0]?.onMessage(outputBatch('old-store', 50, 'retired output\n'));
    expect(session.store.getState().sequence).toBe(50);
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'retired output\n',
    ]);

    transport.subscriptions[0]?.onMessage(
      eventBatch([event(2, 'agent_output_chunk', 'replacement output\n')], {
        store_id: 'new-store',
        through_sequence: 2,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );

    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState()).toMatchObject({
      sequence: 2,
      historyAfterSequence: 0,
    });
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'replacement output\n',
    ]);
    await session.close();
  });

  test('rejects an invalid declared floor without mutating the fold', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();
    const before = session.store.getState();

    expect(() => transport.subscriptions[0]?.onMessage(streamBatch('store-1', 2, -1))).toThrow(
      'event_batch.history_after_sequence',
    );

    expect(session.store.getState()).toBe(before);
    await session.close();
  });

  test('keeps the reached history floor across a resumed batch', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    await session.start();
    transport.subscriptions[0]?.onMessage(streamBatch('run-store', 100, 50));
    transport.silentDials = 1;
    const resumed = transport.nextSubscription();

    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    expect(scheduler.runNext()).toBe(true);
    const subscription = await resumed;
    subscription.onMessage(streamBatch('run-store', 101, 0));

    expect(subscription).toMatchObject({
      afterSequence: 100,
      options: {storeId: 'run-store'},
    });
    expect(session.store.getState().sequence).toBe(101);
    expect(session.store.getState().historyAfterSequence).toBe(50);
    await session.close();
  });

  test('keeps a failed snapshot stale until an explicit wake succeeds', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(new Error('snapshot unavailable'));

    await session.start();
    expect(session.getState().status).toBe('stale');
    expect(session.getState().hasSnapshot).toBe(false);
    expect(transport.subscriptions).toHaveLength(1);

    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.getState()).toEqual(healthy());
    expect(transport.requests).toHaveLength(2);

    await session.close();
  });

  test('reports a dead control channel as its own state, not as a stale stream', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();
    expect(session.getState()).toEqual(healthy());
    const published: string[] = [];
    session.subscribe(() => published.push(session.getState().controls.status));

    transport.dropControlChannel(disconnect('gateway restarted'));

    const dead = session.getState();
    // The transcript is still streaming; only the command path is down, and the
    // two are reported as the separate facts they are.
    expect(dead.status).toBe('connected');
    expect(dead.error).toBeNull();
    expect(dead.controls).toEqual(lost('gateway restarted'));
    expect(published).toEqual(['disconnected']);

    transport.recoverControlChannel();
    expect(session.getState()).toEqual(healthy());
    expect(published).toEqual(['disconnected', 'connected']);

    await session.close();
  });

  test('redials the control channel without touching the event stream', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    transport.dropControlChannel(disconnect('gateway restarted'));
    expect(session.getState().controls.status).toBe('disconnected');
    const requests = transport.requests.length;

    session.reconnectControls();

    // The affordance reaches the transport's redial verb and the recovery it
    // produces reaches the session's state. Issuing a request instead would
    // only queue behind the backoff the outage already armed, so a click could
    // change nothing observable for the length of the schedule.
    expect(transport.reconnectCalls).toBe(1);
    expect(session.getState().controls).toEqual({status: 'connected'});
    // And it is the command path only: a live transcript is not resubscribed.
    await settle();
    expect(transport.subscriptions).toHaveLength(1);
    expect(transport.requests).toHaveLength(requests);

    await session.close();
    transport.dropControlChannel(disconnect('gateway restarted'));
    session.reconnectControls();
    expect(transport.reconnectCalls).toBe(1);
  });

  test('publishes a retry in progress so the affordance can say it is working', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    transport.dropControlChannel(disconnect('gateway restarted'));
    expect(session.getState().controls).toEqual(lost('gateway restarted'));

    // A dial starting is a change a frontend renders (the button goes dead
    // while `reconnect()` would no-op), so it must not be swallowed by a dedup
    // that only compares the status.
    transport.retryControlChannel(disconnect('gateway restarted'));
    expect(session.getState().controls).toEqual(lost('gateway restarted', true));

    await session.close();
  });

  test('publishes an outage on an ended run rather than judging it', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(snapshotResponse({status: 'completed'}));

    await session.start();
    expect(session.store.getState().status).toBe('completed');

    // The channel is genuinely undeliverable, so the session says so. Whether
    // that is worth an affordance on a finished run is a presentation judgment
    // and belongs to `connectionBanners`, which withholds the banner here; see
    // `banners.test.ts`. Deciding it at report time instead latches the banner
    // on, because a drop reported while the run's terminal event is still in
    // flight never gets re-examined when that event lands.
    transport.dropControlChannel(disconnect('gateway exited'));
    expect(session.getState().controls).toEqual(lost('gateway exited'));

    await session.close();
  });

  test('clears a controls banner while a lifecycle refresh finds the run ended', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    transport.dropControlChannel(disconnect('gateway restarted'));
    // Newer than the batch `start()` already folded, so the fold takes it.
    transport.snapshots.push(snapshotResponse({status: 'completed', sequence: 5}));
    const refreshed = waitForSession(session, () => {
      return (
        session.store.getState().status === 'completed' &&
        session.getState().controls.status === 'connected'
      );
    });
    session.reattach();
    await refreshed;
    expect(session.store.getState().status).toBe('completed');
    // The same wake that refreshes the snapshot redials controls first, so the
    // banner raised while the run was live is already down when the terminal
    // snapshot lands.
    expect(session.getState().controls).toEqual({status: 'connected'});

    await session.close();
  });

  test('ignores a control-channel report after the session is closed', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    await session.close();
    transport.dropControlChannel(disconnect('too late'));

    expect(session.getState()).toEqual(healthy());
  });
});

/** Let the transport's pending callbacks run. */
async function settle(): Promise<void> {
  await new Promise<void>(resolve => setTimeout(resolve, 0));
  await Promise.resolve();
}
