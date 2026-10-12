/**
 * The live session and the run view's banners together: what the page says when the stream or the
 * control channel fails, at the seam where the symptom is visible. The session's own behavior is
 * tested in `@vibesys/run-session`; the banner rules in `@vibesys/feature-run-view`.
 */
import {describe, expect, test} from 'bun:test';
import {BackendClientError, type RunStatus, ServerError} from '@vibesys/contracts';
import {eventBatch, snapshotResponse} from '@vibesys/contracts/testing';
import {connectionBanners, STREAM_BANNER_COPY} from '@vibesys/feature-run-view/state';
import {FakeLifecycle, healthy, sessionWith, waitForSession} from '@vibesys/run-session/testing';
import {FakeClock} from '@vibesys/transport/testing';

describe('the run page over a live session', () => {
  /**
   * The #1044 regression, at the seam where the symptom is visible: a run
   * reopened after it finished, whose stream faults before its bootstrap batch.
   *
   * `start()` awaits the snapshot before it subscribes, so the terminal status
   * is always in the store by the time the socket can fault, and the fold is
   * empty because a snapshot carries status and no events. Suppressing the
   * report left the page with nothing but a `completed` chip over an empty
   * transcript.
   */
  test('says the transcript stopped short when the stream faults on an ended run', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(snapshotResponse({sequence: 0, status: 'completed'}));
    transport.silentDials = 1;

    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    initial.onMessage(
      eventBatch([], {store_id: 'store-1', through_sequence: 0, history_after_sequence: 0}),
    );
    await starting;
    expect(session.store.getState().status).toBe('completed');
    // Terminal status, nothing folded: a snapshot carries the status and no
    // events, so `reduceSnapshot` never advances the cursor or the transcript.
    expect(session.store.getState().sequence).toBe(0);
    expect(session.store.getState().transcript).toEqual([]);
    expect(session.getState()).toEqual(healthy());

    // The error `WebSocketTransport` injects when `parseServerMessage` rejects
    // a live frame, delivered where it delivers it: `onDisconnect` on an
    // already-subscribed socket.
    const parseFailure = new BackendClientError('parse', 'Invalid event batch message');
    transport.subscriptions[0]?.onDisconnect(parseFailure);

    expect(session.getState()).toEqual({
      hasSnapshot: true,
      status: 'stale',
      error: parseFailure,
      iterationError: null,
      controls: {status: 'connected'},
    });
    // The page says the transcript is short and offers the explicit recovery
    // that can reload an ended run. The command path itself is still fine.
    expect(connectionBanners(session.store.getState(), session.getState())).toEqual({
      page: null,
      stream: {message: STREAM_BANNER_COPY.ended, reattach: true},
      controls: null,
    });

    // Automatic redial policy for an ended run is unchanged. The user action
    // is different: establish a fresh bootstrap, then load its fenced snapshot
    // instead of asking the terminal-status predicate for permission to resume.
    expect(transport.subscriptions).toHaveLength(1);
    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'completed'}));
    const recovered = waitForSession(session, () => {
      return (
        session.getState().status === 'connected' &&
        session.store.getState().sequence === 1 &&
        transport.subscriptions.length > 1
      );
    });
    session.reattach();
    await recovered;
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.store.getState().status).toBe('completed');

    await session.close();
  });

  test('surfaces a stream protocol error and re-bootstraps an ended run on request', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(snapshotResponse({sequence: 0, status: 'completed'}));
    transport.silentDials = 1;

    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    initial.onMessage(
      eventBatch([], {store_id: 'store-1', through_sequence: 0, history_after_sequence: 0}),
    );
    await starting;
    transport.subscriptions[0]?.onMessage({
      type: 'protocol_error',
      code: 'stream_failed',
      message: 'Replay failed',
      diagnostic: {
        id: 'stream-1',
        code: 'stream_failed',
        summary: 'The replay stream failed.',
        scope: 'protocol',
      },
    });

    expect(session.getState().error).toBeInstanceOf(ServerError);
    expect(session.getState()).toMatchObject({
      status: 'stale',
      error: {
        message: 'Replay failed',
        diagnostic: {id: 'stream-1', code: 'stream_failed', scope: 'protocol'},
      },
    });
    expect(connectionBanners(session.store.getState(), session.getState())).toEqual({
      page: null,
      stream: {message: STREAM_BANNER_COPY.ended, reattach: true},
      controls: null,
    });

    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'completed'}));
    const recovered = waitForSession(session, () => {
      return (
        session.getState().status === 'connected' &&
        session.store.getState().sequence === 1 &&
        transport.subscriptions.length > 1
      );
    });
    session.reattach();
    await recovered;
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.store.getState().status).toBe('completed');

    await session.close();
  });

  test('offers a reattach for the same fault while the run can still stream', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });

    await session.start();
    expect(session.store.getState().sequence).toBe(1);

    const redialed = transport.nextSubscription();
    transport.subscriptions[0]?.onDisconnect(
      new BackendClientError('parse', 'Invalid event batch message'),
    );

    // Read before the redial settles, which is where the banner is on screen:
    // the drop is published in the same task as the fault. Same fault as above
    // and a different statement, because this gap can still close, and the
    // affordance that asks for it sooner rides inside the banner.
    expect(connectionBanners(session.store.getState(), session.getState())).toEqual({
      page: null,
      stream: {message: STREAM_BANNER_COPY.live, reattach: true},
      controls: null,
    });

    // The stream redials on its own schedule and the gap closes: the
    // re-bootstrap folds the batch the faulted dial never delivered.
    expect(scheduler.runNext()).toBe(true);
    await redialed;
    await waitForSession(
      session,
      () => session.store.getState().sequence === 2 && session.getState().status === 'connected',
    );
    expect(transport.subscriptions.length).toBeGreaterThan(1);
    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState().sequence).toBe(2);

    await session.close();
  });

  test('keeps reconnect and reattach available for a forward-compatible run status', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(
      snapshotResponse({sequence: 1, status: 'waiting_for_capacity' as RunStatus}),
    );

    await session.start();
    const resumed = transport.nextSubscription();
    const parseFailure = new BackendClientError('parse', 'Invalid event batch message');
    transport.subscriptions[0]?.onDisconnect(parseFailure);

    expect(connectionBanners(session.store.getState(), session.getState())).toEqual({
      page: null,
      stream: {message: STREAM_BANNER_COPY.live, reattach: true},
      controls: null,
    });
    expect(scheduler.pendingDelays()).toEqual([0]);

    expect(scheduler.runNext()).toBe(true);
    await resumed;
    expect(transport.subscriptions).toHaveLength(2);

    await session.close();
  });
});
