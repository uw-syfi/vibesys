import {describe, expect, test} from 'bun:test';
import {
  type ProtocolResponse,
  type RunSnapshot,
  type RunStatus,
  ServerError,
  type Span,
} from '@vibesys/contracts';
import {event, eventBatch, snapshotResponse} from '@vibesys/contracts/testing';
import {initialIterationNavigation} from '@vibesys/run-state';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import type {BatchReconciliation, StreamMessageContext} from '@vibesys/transport';
import {FakeClock} from '@vibesys/transport/testing';
import {connectRun} from './connect.js';
import type {RunCommandOutcome, RunConnection, RunStreamHandlers} from './ports.js';
import type {RunTracePublication} from './run.js';
import {
  deferred,
  disconnect,
  FakeLifecycle,
  FakeTransport,
  healthy,
  lost,
  outputBatch,
  sessionWith,
  streamBatch,
  waitForSession,
} from './testing/index.js';
import {WebSession} from './web-session.js';

function traceSpan(id: string): Span {
  return {id, parent: null, type: 'flow', name: id, status: 'done', started_at: 1};
}

function spanStarted(sequence: number, id: string) {
  return event(sequence, 'span_started', {
    run_id: 'run-1',
    data: {kind: 'span_started', span: traceSpan(id)},
  });
}

/** A connection whose replacement batch arrives before its subscription handle is adopted. */
class PendingHandleConnection implements RunConnection {
  readonly failure = new Error('replacement snapshot failed');
  #snapshot = 0;
  #release: (() => void) | null = null;

  loadSnapshot(): Promise<RunSnapshot> {
    this.#snapshot += 1;
    return this.#snapshot === 1
      ? Promise.reject(this.failure)
      : Promise.resolve({run_id: 'run-1', sequence: 1, status: 'running'});
  }

  loadIterations(): Promise<{readonly kind: 'unsupported'}> {
    return Promise.resolve({kind: 'unsupported'});
  }

  subscribe(handlers: RunStreamHandlers): Promise<void> {
    const reconciliation: BatchReconciliation = {kind: 'rebootstrap', historyFloor: 0};
    const context: StreamMessageContext = {resumed: false, connectionGeneration: 1};
    handlers.onBatch(
      eventBatch([event(1, 'server_ready')], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
      }),
      reconciliation,
      context,
    );
    return new Promise(resolve => {
      this.#release = resolve;
    });
  }

  retry(): void {}
  retryIfDisconnected(): void {}
  rebootstrap(): 'current' {
    return 'current';
  }
  redialControls(): void {}
  createChatThread(): Promise<never> {
    return Promise.reject(new Error('unused'));
  }

  ask(): Promise<never> {
    return Promise.reject(new Error('Chat is not used in this lifecycle scenario'));
  }
  command(): Promise<RunCommandOutcome> {
    return Promise.resolve({kind: 'rejected', message: null});
  }
  close(): Promise<void> {
    return Promise.resolve();
  }

  release(): void {
    this.#release?.();
  }
}

/** A replacement that faults after connecting but before its fenced snapshot settles. */
class FaultBeforeSnapshotConnection implements RunConnection {
  readonly firstFailure = new Error('first fenced snapshot failed');
  readonly streamFault = new Error('replacement stream sent an invalid frame');
  readonly rebootstrapStarted: Promise<void>;
  #announceRebootstrap: () => void = () => undefined;
  #handlers: RunStreamHandlers | null = null;
  #snapshots = 0;
  #resolveSnapshot: (snapshot: RunSnapshot) => void = () => undefined;

  constructor() {
    this.rebootstrapStarted = new Promise(resolve => {
      this.#announceRebootstrap = resolve;
    });
  }

  loadSnapshot(): Promise<RunSnapshot> {
    this.#snapshots += 1;
    if (this.#snapshots === 1) return Promise.reject(this.firstFailure);
    return new Promise(resolve => {
      this.#resolveSnapshot = resolve;
    });
  }

  loadIterations(): Promise<{readonly kind: 'unsupported'}> {
    return Promise.resolve({kind: 'unsupported'});
  }

  subscribe(handlers: RunStreamHandlers): Promise<void> {
    this.#handlers = handlers;
    this.#batch('store-a', 1);
    return Promise.resolve();
  }

  retry(): void {}
  retryIfDisconnected(): void {}
  rebootstrap(): 'transitioning' {
    this.#announceRebootstrap();
    return 'transitioning';
  }
  redialControls(): void {}
  createChatThread(): Promise<never> {
    return Promise.reject(new Error('unused'));
  }

  ask(): Promise<never> {
    return Promise.reject(new Error('Chat is not used in this lifecycle scenario'));
  }
  command(): Promise<RunCommandOutcome> {
    return Promise.resolve({kind: 'rejected', message: null});
  }
  close(): Promise<void> {
    return Promise.resolve();
  }

  establishFaultedReplacement(): void {
    this.#batch('store-b', 2);
    this.#handlers?.onConnectionState({status: 'connected'});
    this.#handlers?.onFault(this.streamFault);
  }

  resolveReplacementSnapshot(): void {
    this.#resolveSnapshot({run_id: 'run-1', sequence: 1, status: 'running'});
  }

  #batch(storeId: string, connectionGeneration: number): void {
    const reconciliation: BatchReconciliation = {kind: 'rebootstrap', historyFloor: 0};
    this.#handlers?.onBatch(
      eventBatch([event(1, 'server_ready')], {
        store_id: storeId,
        through_sequence: 1,
        history_after_sequence: 0,
      }),
      reconciliation,
      {resumed: false, connectionGeneration},
    );
  }
}

/** A connection whose redials and fresh bootstraps fail when the test queues a failure. */
class FailingRecoveryConnection implements RunConnection {
  readonly redialFailures: Error[] = [];
  readonly rebootstrapFailures: Error[] = [];
  rebootstraps = 0;
  closeCalls = 0;

  loadSnapshot(): Promise<RunSnapshot> {
    return Promise.resolve({run_id: 'run-1', sequence: 1, status: 'running'});
  }

  loadIterations(): Promise<{readonly kind: 'unsupported'}> {
    return Promise.resolve({kind: 'unsupported'});
  }

  subscribe(handlers: RunStreamHandlers): Promise<void> {
    handlers.onBatch(
      eventBatch([event(1, 'server_ready')], {
        store_id: 'store-a',
        through_sequence: 1,
        history_after_sequence: 0,
      }),
      {kind: 'rebootstrap', historyFloor: 0},
      {resumed: false, connectionGeneration: 1},
    );
    handlers.onConnectionState({status: 'connected'});
    return Promise.resolve();
  }

  retry(): void {}
  retryIfDisconnected(): void {}
  rebootstrap(): 'transitioning' {
    this.rebootstraps += 1;
    const failure = this.rebootstrapFailures.shift();
    if (failure !== undefined) throw failure;
    return 'transitioning';
  }
  redialControls(): void {
    const failure = this.redialFailures.shift();
    if (failure !== undefined) throw failure;
  }
  createChatThread(): Promise<never> {
    return Promise.reject(new Error('unused'));
  }

  ask(): Promise<never> {
    return Promise.reject(new Error('Chat is not used in this lifecycle scenario'));
  }
  command(): Promise<RunCommandOutcome> {
    return Promise.resolve({kind: 'rejected', message: null});
  }
  close(): Promise<void> {
    this.closeCalls += 1;
    return Promise.resolve();
  }
}

describe('WebSession robustness', () => {
  test('a Reattach survives a failed redial and a failed fresh bootstrap', async () => {
    const connection = new FailingRecoveryConnection();
    const session = new WebSession({lifecycle: new FakeLifecycle(), connect: () => connection});
    await session.start();
    expect(session.getState()).toEqual(healthy());

    // The refresh's control redial throws: the refresh reports it, and the flight is released.
    const redialFailure = new Error('control redial failed');
    connection.redialFailures.push(redialFailure);
    session.reattach();
    await waitForSession(session, () => session.getState().error === redialFailure);
    expect(connection.rebootstraps).toBe(0);

    // The stream's fresh bootstrap throws: a stream fault, which releases the establishing flight.
    const rebootstrapFailure = new Error('fresh bootstrap failed');
    connection.rebootstrapFailures.push(rebootstrapFailure);
    session.reattach();
    await waitForSession(session, () => session.getState().error === rebootstrapFailure);
    expect(connection.rebootstraps).toBe(1);

    // Neither failure stopped the session or left a flight owning recovery.
    session.reattach();
    await settle();
    expect(connection.rebootstraps).toBe(2);
    await session.close();
    expect(connection.closeCalls).toBe(1);
  });

  test('a throwing Reconnect is a stream fault, not a stopped session', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();

    const failure = new Error('control reconnect failed');
    transport.reconnectFailures.push(failure);
    expect(() => session.reconnectControls()).not.toThrow();
    expect(session.getState()).toMatchObject({status: 'stale', error: failure});

    // The session still recovers when asked.
    const fresh = transport.nextSubscription();
    session.reattach();
    await expect(fresh).resolves.toMatchObject({afterSequence: 0, options: undefined});
    await waitForSession(session, () => session.getState().status === 'connected');
    await session.close();
  });

  test('an action that throws stops the session, releases it, and settles start', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    const snapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(snapshot.promise);
    const starting = session.start();
    await settle();
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(1);

    // A run-store subscriber that throws while the start-up snapshot folds: the machine's
    // applySnapshot action throws, so the machine stops with an error.
    const failure = new Error('view failed to render the snapshot');
    session.store.subscribe(() => {
      throw failure;
    });
    snapshot.resolve(snapshotResponse({sequence: 1, status: 'running'}));
    await starting;
    await settle();
    expect(session.store.getState().status).toBe('running');
    expect(transport.closeCalls).toBe(1);
    expect(lifecycle.listenerCount('online')).toBe(0);
    expect(lifecycle.listenerCount('visibilitychange')).toBe(0);
    expect(lifecycle.listenerCount('offline')).toBe(0);

    // A stopped session asks for nothing more, and close still returns the one release.
    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(1);
    await session.close();
    expect(transport.closeCalls).toBe(1);
  });

  test('close releases the connection even when the session never started', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.close();
    await session.close();
    expect(transport.closeCalls).toBe(1);
    await session.start();
    expect(transport.subscriptions).toHaveLength(0);
  });

  test('close cancels a pending initial subscription without waiting for its deadline', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.deferNextSubscribe();
    const subscriptionStarted = transport.nextSubscription();
    const starting = session.start();
    await subscriptionStarted;

    const closing = session.close();

    // The event stream is fenced first, then transport cancellation starts in
    // the same turn. No scheduler deadline or manual handshake release is part
    // of teardown, and repeated callers join the same terminal operation.
    expect(transport.closeCalls).toBe(1);
    await Promise.all([starting, closing, session.close()]);
    expect(transport.closeCalls).toBe(1);
    expect(lifecycle.listenerCount('online')).toBe(0);
    expect(lifecycle.listenerCount('visibilitychange')).toBe(0);
    expect(lifecycle.listenerCount('offline')).toBe(0);
  });

  test('a report made while the connection opens reaches the session', () => {
    const outage = disconnect('down when the page opened');
    const session = new WebSession({
      lifecycle: new FakeLifecycle(),
      connect: connectRun(hooks => {
        const transport = new FakeTransport(hooks);
        transport.dropControlChannel(outage);
        return transport;
      }),
    });
    expect(session.getState().controls).toEqual({
      status: 'disconnected',
      error: outage,
      everConnected: true,
      retrying: false,
    });
    void session.close();
  });

  test('drops replies and reports that arrive after close', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    await session.start();
    const subscription = transport.subscriptions[0];
    if (subscription === undefined) throw new Error('Initial subscription did not open');

    const refresh = deferred<ProtocolResponse>();
    transport.snapshots.push(refresh.promise);
    lifecycle.emit('online');
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(2);
    await session.close();
    const closed = session.getState();
    const published: string[] = [];
    session.subscribe(() => published.push(session.getState().status));

    refresh.resolve(snapshotResponse({sequence: 5, status: 'completed'}));
    await settle();
    subscription.onDisconnect(disconnect('closed session'));
    transport.dropControlChannel(disconnect('closed session'));
    await settle();

    expect(session.store.getState().status).not.toBe('completed');
    expect(session.getState()).toBe(closed);
    expect(published).toEqual([]);
  });

  test('aborts each snapshot request when a replacement or close supersedes its actor', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();
    const subscription = transport.subscriptions[0];
    if (subscription === undefined) throw new Error('Initial subscription did not open');

    const storeTwo = deferred<ProtocolResponse>();
    const storeThree = deferred<ProtocolResponse>();
    transport.snapshots.push(storeTwo.promise, storeThree.promise);
    subscription.onMessage(
      eventBatch([event(1, 'server_ready')], {
        store_id: 'store-2',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await settle();

    const firstIndex = transport.requests.findIndex(
      (request, index) =>
        index > 0 && request.type === 'query.snapshot' && request.expected_store_id === 'store-2',
    );
    expect(firstIndex).toBeGreaterThan(0);
    const firstSignal = transport.requestOptions[firstIndex]?.signal;
    expect(firstSignal?.aborted).toBe(false);

    subscription.onMessage(
      eventBatch([event(1, 'server_ready')], {
        store_id: 'store-3',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await settle();

    const secondIndex = transport.requests.findIndex(
      request => request.type === 'query.snapshot' && request.expected_store_id === 'store-3',
    );
    expect(secondIndex).toBeGreaterThan(firstIndex);
    const secondSignal = transport.requestOptions[secondIndex]?.signal;
    expect(firstSignal?.aborted).toBe(true);
    expect(secondSignal?.aborted).toBe(false);

    await session.close();
    expect(secondSignal?.aborted).toBe(true);

    // This Fake deliberately ignores cancellation and may settle late. Issue fencing still keeps
    // those replies inert, while production transports release the request when the signal aborts.
    storeTwo.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    storeThree.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await settle();
  });
});

describe('WebSession', () => {
  test('loads policy iteration navigation into the shared store', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.iterations.push(iterationResponse('dynamic-1', 'Generation 1'));

    await session.start();

    expect(transport.requests).toContainEqual({
      type: 'query.iterations',
      expected_store_id: 'store-1',
    });
    expect(session.store.getIterations().byId['generation:1']).toMatchObject({
      kind: 'generation',
      label: 'Generation 1',
    });
    await session.close();
  });

  test('rejects foreign iteration navigation without crashing or replacing the run view', async () => {
    const {session, transport} = sessionWith(new FakeLifecycle());
    transport.iterations.push(iterationResponse('foreign', 'Generation 2', 'run-2'));

    await session.start();
    await waitForSession(session, () => session.getState().iterationError !== null);

    expect(session.store.getState().runId).toBe('run-1');
    expect(session.store.getIterations()).toEqual(initialIterationNavigation());
    expect(session.getState()).toMatchObject({
      status: 'connected',
      error: null,
      controls: {status: 'connected'},
    });
    expect(session.getState().iterationError?.message).toContain('run-1');
    expect(session.getState().iterationError?.message).toContain('run-2');
    await session.close();
  });

  test('delivers a lifecycle command through the session-owned control connection', async () => {
    const {session, transport} = sessionWith(new FakeLifecycle());
    transport.snapshots.push({
      request_id: 'request-pause',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      ack: {action: 'pause', status: 'pending'},
    });

    const outcome = await session.command({type: 'command.pause'});

    expect(transport.requests).toEqual([{type: 'command.pause'}]);
    expect(outcome).toEqual({kind: 'acknowledged', action: 'pause', status: 'pending'});
    await session.close();
  });

  test('turns a rejected command response into the session command outcome', async () => {
    const {session, transport} = sessionWith(new FakeLifecycle());
    transport.snapshots.push({
      request_id: 'request-stop',
      timestamp: '2026-01-01T00:00:00Z',
      ok: false,
      error: 'run is read only',
    });

    await expect(session.command({type: 'command.stop'})).resolves.toEqual({
      kind: 'rejected',
      message: 'run is read only',
      code: null,
    });
    await session.close();
  });

  test('a refused command carries the server typed reason', async () => {
    const {session, transport} = sessionWith(new FakeLifecycle());
    transport.snapshots.push({
      request_id: 'request-stop',
      timestamp: '2026-01-01T00:00:00Z',
      ok: false,
      error: 'This finished run is read-only',
      diagnostic: {
        code: 'run_read_only',
        summary: 'This finished run is read-only',
        scope: 'request',
      },
    });

    await expect(session.command({type: 'command.stop'})).resolves.toEqual({
      kind: 'rejected',
      message: 'This finished run is read-only',
      code: 'run_read_only',
    });
    await session.close();
  });

  test('waits for initial stream identity before loading iteration navigation', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.silentDials = 1;
    transport.deferNextSubscribe();

    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;

    expect(transport.requests.some(request => request.type === 'query.iterations')).toBe(false);

    transport.releasePendingSubscribe();
    await settle();
    expect(transport.requests.some(request => request.type === 'query.iterations')).toBe(false);

    initial.onMessage(streamBatch('store-1', 1));
    await starting;

    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      1,
    );
    await session.close();
  });

  test('refreshes policy navigation on a semantic iteration invalidation', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.iterations.push(iterationResponse('evolve-0', 'Generation 0'));
    await session.start();
    transport.iterations.push(iterationResponse('evolve-1', 'Generation 1'));
    const refreshed = waitForSession(
      session,
      () => session.store.getIterations().revision === 'evolve-1',
    );

    transport.subscriptions[0]?.onMessage(
      eventBatch(
        [
          event(2, 'iterations_changed', {
            data: {kind: 'iterations_changed', revision: 'evolve-1'},
          }),
        ],
        {through_sequence: 2, store_id: 'store-1', history_after_sequence: 0},
      ),
    );

    await refreshed;
    expect(session.store.getIterations().byId['generation:1']?.label).toBe('Generation 1');
    await session.close();
  });

  test('suppresses an initial iteration result superseded by an invalidation', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    const initial = deferred<ProtocolResponse>();
    const current = deferred<ProtocolResponse>();
    transport.iterations.push(initial.promise, current.promise);
    transport.deferNextSubscribe();
    const revisions: Array<string | null> = [];
    const unsubscribe = session.store.subscribe(() => {
      revisions.push(session.store.getIterations().revision);
    });

    const started = session.start();
    await settle();
    const subscription = transport.subscriptions[0];
    expect(subscription).toBeDefined();
    subscription?.onMessage(
      eventBatch(
        [
          event(2, 'iterations_changed', {
            data: {kind: 'iterations_changed', revision: 'evolve-1'},
          }),
        ],
        {through_sequence: 2, store_id: 'store-1', history_after_sequence: 0},
      ),
    );
    initial.resolve(iterationResponse('evolve-0', 'Generation 0'));
    await settle();
    expect(session.store.getIterations().revision).toBeNull();
    expect(revisions).not.toContain('evolve-0');

    current.resolve(iterationResponse('evolve-1', 'Generation 1'));
    transport.releasePendingSubscribe();
    await started;

    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      2,
    );
    expect(revisions).not.toContain('evolve-0');
    expect(session.store.getIterations().byId['generation:1']?.label).toBe('Generation 1');
    unsubscribe();
    await session.close();
  });

  test('suppresses an initial iteration failure superseded by a successful invalidation', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    let rejectPage!: (error: unknown) => void;
    const failedPage = new Promise<ProtocolResponse>((_resolve, reject) => {
      rejectPage = reject;
    });
    const current = deferred<ProtocolResponse>();
    transport.iterations.push(
      iterationPageResponse('stale', null, 0, 100, 'page-2'),
      failedPage,
      current.promise,
    );
    transport.deferNextSubscribe();

    const started = session.start();
    await settle();
    transport.subscriptions[0]?.onMessage(
      eventBatch(
        [
          event(2, 'iterations_changed', {
            data: {kind: 'iterations_changed', revision: 'evolve-1'},
          }),
        ],
        {through_sequence: 2, store_id: 'store-1', history_after_sequence: 0},
      ),
    );

    rejectPage(new ServerError('superseded iteration failure'));
    await settle();
    expect(session.getState().error).toBeNull();

    current.resolve(iterationResponse('evolve-1', 'Generation 1'));
    transport.releasePendingSubscribe();
    await started;

    expect(session.getState().error).toBeNull();
    expect(session.store.getIterations().revision).toBe('evolve-1');
    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      3,
    );
    await session.close();
  });

  test('reloads iteration navigation from the stream store that replaces an in-flight query', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.iterations.push(iterationResponse('store-a', 'Generation 1'));
    await session.start();
    const stale = deferred<ProtocolResponse>();
    const current = deferred<ProtocolResponse>();
    transport.iterations.push(stale.promise, current.promise);
    const stream = transport.subscriptions[0];
    if (stream === undefined) throw new Error('Initial subscription did not open');

    stream.onMessage(
      eventBatch(
        [
          event(2, 'iterations_changed', {
            data: {kind: 'iterations_changed', revision: 'store-a-late'},
          }),
        ],
        {through_sequence: 2, store_id: 'store-1', history_after_sequence: 0},
      ),
    );
    stream.onMessage(
      eventBatch([event(1, 'server_ready')], {
        through_sequence: 1,
        store_id: 'store-2',
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );

    stale.resolve(iterationResponse('store-a-late', 'Generation 2'));
    await settle();
    expect(session.store.getIterations().revision).toBe('store-a');

    current.resolve(iterationResponse('store-b', 'Generation 3'));
    await waitForSession(session, () => session.store.getIterations().revision === 'store-b');
    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      3,
    );
    await session.close();
  });

  test('suppresses an old-control iteration rejection until the stream proves the next store', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.iterations.push(iterationResponse('store-a', 'Generation 1'));
    await session.start();
    transport.iterations.push(
      iterationStoreReplacedError(),
      iterationResponse('store-c', 'Generation 3'),
    );
    const stream = transport.subscriptions[0];
    if (stream === undefined) throw new Error('Initial subscription did not open');

    stream.onMessage(
      eventBatch([event(1, 'server_ready')], {
        through_sequence: 1,
        store_id: 'store-2',
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await settle();

    expect(session.store.getIterations().revision).toBe('store-a');
    expect(session.getState().error).toBeNull();

    stream.onMessage(
      eventBatch([event(1, 'server_ready')], {
        through_sequence: 1,
        store_id: 'store-3',
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await waitForSession(session, () => session.store.getIterations().revision === 'store-c');

    expect(
      transport.requests.filter(request => request.type === 'query.iterations').slice(-2),
    ).toEqual([
      {type: 'query.iterations', expected_store_id: 'store-2'},
      {type: 'query.iterations', expected_store_id: 'store-3'},
    ]);
    expect(session.getState().error).toBeNull();
    await session.close();
  });

  test('does not mistake an initial store fence for an unsupported iteration capability', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.iterations.push(iterationStoreReplacedError());

    await session.start();

    expect(session.store.getIterations().revision).toBeNull();
    expect(session.getState().error).toBeNull();

    transport.iterations.push(iterationResponse('store-b', 'Generation 2'));
    transport.subscriptions[0]?.onMessage(
      eventBatch([event(1, 'server_ready')], {
        through_sequence: 1,
        store_id: 'store-2',
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await waitForSession(session, () => session.store.getIterations().revision === 'store-b');

    expect(transport.requests.filter(request => request.type === 'query.iterations')).toEqual([
      {type: 'query.iterations', expected_store_id: 'store-1'},
      {type: 'query.iterations', expected_store_id: 'store-2'},
    ]);
    await session.close();
  });

  test('publishes more than one hundred iteration roots only after the last page', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    const lastPage = deferred<ProtocolResponse>();
    transport.iterations.push(
      iterationPageResponse('bulk', null, 0, 100, 'page-2'),
      lastPage.promise,
    );

    const started = session.start();
    await settle();
    expect(session.store.getIterations().revision).toBeNull();
    expect(Object.keys(session.store.getIterations().byId)).toHaveLength(0);

    lastPage.resolve(iterationPageResponse('bulk', 'page-2', 100, 25, null));
    await started;

    expect(session.store.getIterations().revision).toBe('bulk');
    expect(Object.keys(session.store.getIterations().byId)).toHaveLength(125);
    expect(transport.requests.filter(request => request.type === 'query.iterations')).toEqual([
      {type: 'query.iterations', expected_store_id: 'store-1'},
      {type: 'query.iterations', expected_store_id: 'store-1', cursor: 'page-2'},
    ]);
    await session.close();
  });

  test('retains the last complete iteration tree when a later page fails', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.iterations.push(iterationResponse('complete', 'Generation 0'));
    await session.start();
    transport.iterations.push(
      iterationPageResponse('replacement', null, 0, 100, 'page-2'),
      new ServerError('second iteration page failed'),
    );
    const failed = waitForSession(
      session,
      () => session.getState().iterationError?.message === 'second iteration page failed',
    );

    transport.subscriptions[0]?.onMessage(
      eventBatch(
        [
          event(2, 'iterations_changed', {
            data: {kind: 'iterations_changed', revision: 'replacement'},
          }),
        ],
        {through_sequence: 2, store_id: 'store-1', history_after_sequence: 0},
      ),
    );
    await failed;

    expect(session.store.getIterations().revision).toBe('complete');
    expect(Object.keys(session.store.getIterations().byId)).toEqual(['generation:0']);
    expect(session.getState()).toMatchObject({status: 'connected', error: null});
    await session.close();
  });

  test('retains the last complete iteration tree when paging repeats a cursor', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.iterations.push(iterationResponse('complete', 'Generation 0'));
    await session.start();
    transport.iterations.push(
      iterationPageResponse('replacement', null, 0, 100, 'page-2'),
      iterationPageResponse('replacement', 'page-2', 100, 1, 'page-2'),
    );
    const failed = waitForSession(
      session,
      () =>
        session.getState().iterationError?.message === 'iteration query repeated a paging cursor',
    );

    transport.subscriptions[0]?.onMessage(
      eventBatch(
        [
          event(2, 'iterations_changed', {
            data: {kind: 'iterations_changed', revision: 'replacement'},
          }),
        ],
        {through_sequence: 2, store_id: 'store-1', history_after_sequence: 0},
      ),
    );
    await failed;

    expect(session.store.getIterations().revision).toBe('complete');
    expect(Object.keys(session.store.getIterations().byId)).toEqual(['generation:0']);
    expect(session.getState()).toMatchObject({status: 'connected', error: null});
    await session.close();
  });

  test('close aborts iteration paging before a late first page can request page two', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    const firstPage = deferred<ProtocolResponse>();
    transport.iterations.push(firstPage.promise);

    const starting = session.start();
    await settle();
    const requestIndex = transport.requests.findIndex(
      request => request.type === 'query.iterations',
    );
    expect(requestIndex).toBeGreaterThanOrEqual(0);
    const signal = transport.requestOptions[requestIndex]?.signal;
    expect(signal?.aborted).toBe(false);

    await session.close();
    expect(signal?.aborted).toBe(true);
    firstPage.resolve(iterationPageResponse('late', null, 0, 100, 'page-2'));
    await starting;
    await settle();

    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      1,
    );
  });

  test('falls back only when the initial iteration capability probe is rejected', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.iterations.push(new ServerError('Unsupported protocol request'));

    await session.start();
    expect(session.getState()).toEqual(healthy());
    transport.subscriptions[0]?.onMessage(
      eventBatch(
        [
          event(2, 'iterations_changed', {
            data: {kind: 'iterations_changed', revision: 'ignored'},
          }),
        ],
        {through_sequence: 2, store_id: 'store-1', history_after_sequence: 0},
      ),
    );
    await settle();

    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      1,
    );
    expect(session.getState()).toEqual(healthy());
    await session.close();
  });

  test('keeps a foreign fenced snapshot from proving the streamed run healthy', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(snapshotResponse({run_id: 'run-b', sequence: 1, status: 'completed'}));
    transport.silentDials = 1;

    const dialed = transport.nextSubscription();
    const starting = session.start();
    const initial = await dialed;
    initial.onMessage(
      eventBatch([event(1, 'server_ready', {run_id: 'run-a'})], {
        store_id: 'store-1',
        through_sequence: 1,
        history_after_sequence: 0,
      }),
    );
    await starting;

    expect(session.store.getState()).toMatchObject({
      runId: 'run-a',
      sequence: 1,
      diagnostics: [{code: 'run_identity_mismatch'}],
    });
    expect(session.getState()).toMatchObject({status: 'stale', hasSnapshot: false});

    await session.close();
  });

  test('keeps a foreign same-store event batch stale after run-state rejects it', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(snapshotResponse({run_id: 'run-a', sequence: 1}));
    await session.start();

    transport.subscriptions[0]?.onMessage(
      eventBatch([event(2, 'server_ready', {run_id: 'run-b'})], {
        through_sequence: 2,
        store_id: 'store-1',
        history_after_sequence: 0,
      }),
    );

    expect(session.store.getState()).toMatchObject({
      runId: 'run-a',
      sequence: 1,
      diagnostics: [{code: 'run_identity_mismatch'}],
    });
    expect(session.getState().status).toBe('stale');

    await session.close();
  });

  test('a refresh that loads another run fails the load and keeps the page stale', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(snapshotResponse({run_id: 'run-a', sequence: 1}));
    await session.start();
    expect(session.getState()).toEqual(healthy());

    // Each refresh retries the stream; keep those dials silent so only the snapshots decide.
    transport.silentDials = 2;
    transport.snapshots.push(snapshotResponse({run_id: 'run-b', sequence: 5, status: 'completed'}));
    lifecycle.emit('online');
    await waitForSession(session, () => session.getState().status === 'stale');
    expect(session.store.getState()).toMatchObject({
      runId: 'run-a',
      sequence: 1,
      diagnostics: [{code: 'run_identity_mismatch'}],
    });
    expect(session.getState()).toMatchObject({hasSnapshot: true, status: 'stale'});

    // A later load of the streamed run proves it again.
    transport.snapshots.push(snapshotResponse({run_id: 'run-a', sequence: 1}));
    lifecycle.emit('online');
    await waitForSession(session, () => session.getState().status === 'connected');
    expect(session.getState().error).toBeNull();

    await session.close();
  });

  test('keeps a newer terminal watermark when a stale same-run refresh is rejected', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));

    await session.start();
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');

    // Core-state rejects this same-run response's stale liveness: it cannot un-end a fold at the
    // same cursor. It must not lower the separate evidence that sequence 5 exists in this log.
    const stale = deferred<ProtocolResponse>();
    transport.snapshots.push(stale.promise);
    transport.silentDials = 1;
    const resumedDial = transport.nextSubscription();
    lifecycle.emit('online');
    stale.resolve(snapshotResponse({sequence: 1, status: 'running'}));
    const resumed = await resumedDial;

    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    expect(initial.closed).toBe(true);
    expect(transport.subscriptions).toHaveLength(2);
    expect(resumed).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});

    resumed.onDisconnect(disconnect('stream ended before the known tail'));
    expect(session.getState().status).toBe('stale');
    expect(scheduler.pendingDelays()).toEqual([0]);
    await session.close();
  });

  test('retries a quietly closed startup stream when its fenced snapshot proves a gap', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    const startupSnapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(startupSnapshot.promise);
    transport.silentDials = 1;

    const starting = session.start();
    await settle();
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');

    initial.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'store-1',
        through_sequence: 1,
        history_after_sequence: 0,
      }),
    );
    await settle();
    expect(
      transport.requests.filter(request => request.type === 'query.snapshot').at(-1),
    ).toMatchObject({type: 'query.snapshot', expected_store_id: 'store-1'});

    // The terminal fold makes this established close look complete until its snapshot answers.
    initial.onDisconnect(disconnect('startup stream closed before snapshot'));
    expect(transport.subscriptions).toHaveLength(1);

    // The fenced snapshot is authoritative evidence that two journal events are still missing.
    transport.silentDials = 1;
    startupSnapshot.resolve(snapshotResponse({sequence: 3, status: 'completed'}));
    await starting;
    await settle();

    expect(initial.closed).toBe(true);
    expect(transport.subscriptions).toHaveLength(2);
    expect(transport.subscriptions[1]).toMatchObject({
      afterSequence: 1,
      options: {storeId: 'store-1'},
    });
    await session.close();
  });

  test('keeps the old watermark until a fresh batch proves the stream store changed', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();

    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');
    initial.onMessage({type: 'protocol_error', code: 'stream_failed', message: 'Replay failed'});
    expect(session.getState().status).toBe('stale');

    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'running'}));
    transport.silentDials = 1;
    const redialed = transport.nextSubscription();
    session.reattach();
    const replacement = await redialed;
    expect(replacement).toMatchObject({afterSequence: 0, options: undefined});

    // A fresh connection is not necessarily a fresh store. A whole-backlog WebSocket bootstrap
    // may be split into complete event batches, so this first same-store batch can end before the
    // accepted snapshot watermark. Losing the connection here must retain that evidence and retry.
    const connected = waitForSession(session, () => session.getState().status === 'connected');
    replacement.onMessage(streamBatch('store-1', 1));
    await connected;
    replacement.onDisconnect(disconnect('same store ended before sequence 5'));

    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    expect(session.getState().status).toBe('stale');
    expect(scheduler.pendingDelays()).toEqual([0]);
    await session.close();
  });

  test('accepts a lower watermark after the stream store re-bootstraps', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));

    await session.start();
    const subscription = transport.subscriptions[0];
    if (subscription === undefined) throw new Error('Initial subscription did not open');
    const replacementSnapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(replacementSnapshot.promise);
    subscription.onMessage(
      eventBatch([event(1, 'server_ready')], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'connecting'});

    // The pre-rebootstrap sequence-5 snapshot cannot describe the replacement. Its own
    // post-rebootstrap sequence-1 snapshot is authoritative and may lower the watermark.
    const refreshed = nextSessionChange(session);
    replacementSnapshot.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await refreshed;

    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    expect(subscription.closed).toBe(false);
    expect(transport.subscriptions).toHaveLength(1);
    subscription.onDisconnect(disconnect('replacement store complete'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);
    await session.close();
  });

  test('does not assign a pre-bootstrap snapshot to the replacement store', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));

    await session.start();
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');
    initial.onMessage({
      type: 'protocol_error',
      code: 'stream_failed',
      message: 'Replay failed',
    });

    // Reattach establishes its stream store before issuing any snapshot query,
    // so neither an old-store nor an early new-store response can be guessed
    // into the generation from cross-socket delivery order.
    const postReplacement = deferred<ProtocolResponse>();
    transport.snapshots.push(postReplacement.promise);
    transport.silentDials = 1;
    const replacementDial = transport.nextSubscription();
    const connected = waitForSession(session, () => session.getState().status === 'connected');
    session.reattach();
    const replacement = await replacementDial;
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(1);
    replacement.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await connected;
    expect(
      transport.requests.filter(request => request.type === 'query.snapshot').at(-1),
    ).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'replacement-store',
    });
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});

    const validated = nextSessionChange(session);
    postReplacement.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await validated;
    replacement.onDisconnect(disconnect('replacement store complete'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);
    await session.close();
  });

  test('retains post-replacement evidence without replacing a live stream', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');
    initial.onMessage({type: 'protocol_error', code: 'stream_failed', message: 'Replay failed'});

    // The reverse cross-socket ordering has the same handoff: the query still
    // begins only after the replacement batch, fenced to its store.
    const postReplacement = deferred<ProtocolResponse>();
    transport.snapshots.push(postReplacement.promise);
    transport.silentDials = 1;
    const replacementDial = transport.nextSubscription();
    const connected = waitForSession(session, () => session.getState().status === 'connected');
    session.reattach();
    const replacement = await replacementDial;
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(1);
    replacement.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await connected;
    expect(
      transport.requests.filter(request => request.type === 'query.snapshot').at(-1),
    ).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'replacement-store',
    });

    const snapshotApplied = nextSessionChange(session);
    postReplacement.resolve(snapshotResponse({sequence: 3, status: 'completed'}));
    await snapshotApplied;
    expect(replacement.closed).toBe(false);
    expect(transport.subscriptions).toHaveLength(2);

    // The stream can deliver the durable suffix itself. Higher snapshot
    // evidence must not proactively retire a healthy socket just to discover
    // whether it had closed.
    replacement.onMessage(outputBatch('replacement-store', 2, 'second\n'));
    replacement.onMessage(outputBatch('replacement-store', 3, 'third\n'));
    replacement.onDisconnect(disconnect('replacement store complete'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);
    await session.close();
  });

  test('retries a terminal close when its post-replacement snapshot later proves a gap', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');
    initial.onMessage({type: 'protocol_error', code: 'stream_failed', message: 'Replay failed'});

    const postReplacement = deferred<ProtocolResponse>();
    transport.snapshots.push(postReplacement.promise);
    transport.silentDials = 1;
    const replacementDial = transport.nextSubscription();
    const connected = waitForSession(session, () => session.getState().status === 'connected');
    session.reattach();
    const replacement = await replacementDial;
    replacement.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await connected;

    // Before the generation-fenced snapshot returns, the sequence-1 terminal fold is all the
    // client can prove. Its close is quiet. Higher evidence arriving afterward must wake the dead
    // stream immediately instead of waiting for another browser lifecycle event.
    replacement.onDisconnect(disconnect('replacement closed before snapshot'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);

    transport.silentDials = 1;
    const resumedDial = transport.nextSubscription();
    postReplacement.resolve(snapshotResponse({sequence: 3, status: 'completed'}));
    const resumed = await resumedDial;
    expect(replacement.closed).toBe(true);
    expect(resumed).toMatchObject({
      afterSequence: 1,
      options: {storeId: 'replacement-store'},
    });

    resumed.onDisconnect(disconnect('replacement still ends before sequence 3'));
    expect(session.getState().status).toBe('stale');
    expect(scheduler.pendingDelays()).toEqual([0]);
    await session.close();
  });

  test('rebootstraps when the fenced store is replaced before snapshot acquisition', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();
    transport.subscriptions[0]?.onMessage({
      type: 'protocol_error',
      code: 'stream_failed',
      message: 'Replay failed',
    });

    const storeB = deferred<ProtocolResponse>();
    transport.snapshots.push(storeB.promise);
    transport.silentDials = 1;
    const bDial = transport.nextSubscription();
    session.reattach();
    const b = await bDial;
    b.onMessage(streamBatch('store-b', 1));
    expect(
      transport.requests.filter(request => request.type === 'query.snapshot').at(-1),
    ).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'store-b',
    });

    const storeC = deferred<ProtocolResponse>();
    transport.snapshots.push(storeC.promise);
    transport.silentDials = 1;
    const cDial = transport.nextSubscription();
    storeB.resolve(storeReplacedResponse());
    const c = await cDial;
    c.onMessage(streamBatch('store-c', 1));
    expect(
      transport.requests.filter(request => request.type === 'query.snapshot').at(-1),
    ).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'store-c',
    });

    const accepted = waitForSession(session, () => session.store.getState().status === 'completed');
    storeC.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await accepted;
    expect(b.closed).toBe(true);
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(3);
    await waitForSession(session, () => session.getState().status === 'connected');
    await session.close();
  });

  test('drops an older fenced reply when the replacement batch arrives first', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();
    transport.subscriptions[0]?.onMessage({
      type: 'protocol_error',
      code: 'stream_failed',
      message: 'Replay failed',
    });

    const storeB = deferred<ProtocolResponse>();
    transport.snapshots.push(storeB.promise);
    transport.silentDials = 1;
    const bDial = transport.nextSubscription();
    session.reattach();
    const stream = await bDial;
    stream.onMessage(streamBatch('store-b', 1));

    const storeC = deferred<ProtocolResponse>();
    transport.snapshots.push(storeC.promise);
    stream.onMessage(streamBatch('store-c', 1));
    expect(
      transport.requests.filter(request => request.type === 'query.snapshot').slice(-2),
    ).toMatchObject([
      {type: 'query.snapshot', expected_store_id: 'store-b'},
      {type: 'query.snapshot', expected_store_id: 'store-c'},
    ]);

    const accepted = nextSessionChange(session);
    storeC.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await accepted;
    storeB.resolve(storeReplacedResponse());
    await Promise.resolve();
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    await waitForSession(session, () => session.getState().status === 'connected');
    await session.close();
  });

  test('keeps one generation monotone while its fresh subscription handle is pending', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();

    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');
    initial.onMessage({type: 'protocol_error', code: 'stream_failed', message: 'Replay failed'});

    // The first explicit recovery gets a fresh sequence-zero batch before its
    // subscription handle settles. That batch is already the visible generation.
    const replacementSnapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(replacementSnapshot.promise);
    transport.silentDials = 1;
    transport.deferNextSubscribe();
    const replacementDial = transport.nextSubscription();
    session.reattach();
    const replacement = await replacementDial;
    replacement.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    expect(
      transport.requests.filter(request => request.type === 'query.snapshot').at(-1),
    ).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'replacement-store',
    });

    // Repeated Reattach requests share the established generation and its
    // fenced snapshot while the subscription handle is still pending.
    session.reattach();
    session.reattach();
    expect(transport.subscriptions).toHaveLength(2);
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(2);

    const replacementValidated = nextSessionChange(session);
    replacementSnapshot.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await replacementValidated;

    const connected = waitForSession(session, () => session.getState().status === 'connected');
    transport.releasePendingSubscribe();
    await connected;

    // A later in-loop store replacement owns a distinct fenced query.
    const laterSnapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(laterSnapshot.promise);
    replacement.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'later-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    expect(
      transport.requests.filter(request => request.type === 'query.snapshot').at(-1),
    ).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'later-store',
    });
    const laterValidated = nextSessionChange(session);
    laterSnapshot.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await laterValidated;
    replacement.onDisconnect(disconnect('later store complete'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);

    await session.close();
    replacement.onDisconnect(disconnect('closed session'));
    expect(scheduler.runNext()).toBe(false);
  });

  test('resumes when a lifecycle snapshot ends ahead of the event stream', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'running'}));

    await session.start();
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'running'});
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');

    transport.snapshots.push(snapshotResponse({sequence: 3, status: 'completed'}));
    transport.silentDials = 1;
    const resumed = transport.nextSubscription();
    const observedTerminal = waitForSession(
      session,
      () => session.store.getState().status === 'completed',
    );
    lifecycle.emit('online');
    await observedTerminal;

    // The accepted snapshot proves that the current subscription has not delivered the tail.
    // Retire it during lifecycle recovery instead of treating terminal status as complete.
    expect(initial.closed).toBe(true);
    const subscription = await resumed;
    expect(subscription).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});

    const recovered = waitForSession(session, () => {
      return session.getState().status === 'connected' && session.store.getState().sequence === 3;
    });
    subscription.onMessage(outputBatch('store-1', 2, 'last output\n'));
    subscription.onMessage(outputBatch('store-1', 3, 'terminal output\n'));
    await recovered;
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'last output\n',
      'terminal output\n',
    ]);

    // Once the fold reaches the accepted snapshot, an ordinary terminal close stays quiet and
    // does not start a reconnect loop.
    subscription.onDisconnect(disconnect('gateway exited'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);
    expect(transport.subscriptions).toHaveLength(2);

    await session.close();
    subscription.onDisconnect(disconnect('closed session'));
    expect(scheduler.runNext()).toBe(false);
    expect(transport.subscriptions).toHaveLength(2);
  });

  test('terminal stream closes redial exactly while the snapshot watermark is ahead', async () => {
    const terminalStatuses = [
      'completed',
      'failed',
      'stopped',
    ] as const satisfies readonly RunStatus[];

    for (const seed of SEEDS.slice(0, 12)) {
      const random = new Seeded(seed);
      const status = random.pick(terminalStatuses);
      const snapshotSequence = random.int(6) + 2;
      const lifecycle = new FakeLifecycle();
      const scheduler = new FakeClock();
      const {session, transport} = sessionWith(lifecycle, {
        reconnectDelaysMs: [0],
        scheduleTimeout: scheduler.scheduleTimeout,
      });
      transport.snapshots.push(snapshotResponse({sequence: snapshotSequence, status}));

      await session.start();
      expect(session.store.getState()).toMatchObject({sequence: 1, status});
      const initial = transport.subscriptions[0];
      if (initial === undefined) throw new Error(`Seed ${seed}: initial subscription did not open`);
      const failure = disconnect(`seed ${seed}: stream closed`);

      transport.silentDials = 1;
      const retrying = transport.nextSubscription();
      initial.onDisconnect(failure);
      expect({
        seed,
        status: session.getState().status,
        pending: scheduler.pendingDelays(),
      }).toEqual({seed, status: 'stale', pending: [0]});

      expect(scheduler.runNext()).toBe(true);
      const subscription = await retrying;
      expect(subscription).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});

      // A resumed delivery at the existing cursor is ignored, and each missing event is folded
      // once in sequence. The snapshot watermark never advances the event cursor itself.
      subscription.onMessage(outputBatch('store-1', 1, 'duplicate output\n'));
      const recovered = waitForSession(session, () => {
        return (
          session.getState().status === 'connected' &&
          session.store.getState().sequence === snapshotSequence
        );
      });
      const expected: string[] = [];
      for (let sequence = 2; sequence <= snapshotSequence; sequence += 1) {
        const content = `seed ${seed}, event ${sequence}\n`;
        expected.push(content);
        subscription.onMessage(outputBatch('store-1', sequence, content));
      }
      await recovered;
      expect({
        seed,
        transcript: session.store.getState().transcript.map(entry => entry.content),
      }).toEqual({seed, transcript: expected});

      subscription.onDisconnect(disconnect(`seed ${seed}: terminal close`));
      expect({seed, state: session.getState(), pending: scheduler.pendingDelays()}).toEqual({
        seed,
        state: healthy(),
        pending: [],
      });

      await session.close();
      subscription.onDisconnect(disconnect(`seed ${seed}: closed session`));
      expect({
        seed,
        timerRan: scheduler.runNext(),
        subscriptions: transport.subscriptions.length,
      }).toEqual({seed, timerRan: false, subscriptions: 2});
    }
  });

  test('lets an in-flight initial bootstrap satisfy a terminal refresh watermark', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'running'}));
    transport.deferNextSubscribe();

    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    await waitForSession(session, () => session.store.getState().status === 'running');
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'running'});

    transport.snapshots.push(snapshotResponse({sequence: 3, status: 'completed'}));
    const observedTerminal = waitForSession(
      session,
      () => session.store.getState().status === 'completed',
    );
    lifecycle.emit('online');
    await observedTerminal;

    // The initial dial already owns a fresh bootstrap. The lifecycle retry coalesces with it
    // instead of retiring it or opening a parallel socket, and its remaining events can satisfy
    // the snapshot watermark before the handshake returns.
    expect(initial.closed).toBe(false);
    expect(transport.subscriptions).toHaveLength(1);
    initial.onMessage(outputBatch('store-1', 2, 'penultimate output\n'));
    initial.onMessage(outputBatch('store-1', 3, 'last output\n'));
    transport.releasePendingSubscribe();
    await starting;

    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState()).toMatchObject({sequence: 3, status: 'completed'});
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'penultimate output\n',
      'last output\n',
    ]);
    expect(transport.subscriptions).toHaveLength(1);
    await session.close();
  });

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
    // The refresh operation reports its failed redial as its output, a promise step later.
    await waitForSession(session, () => session.getState().error === failure);
    expect(session.getState()).toMatchObject({status: 'stale', error: failure});

    const recovered = transport.nextSubscription();
    session.reattach();
    expect(transport.reconnectCalls).toBe(2);
    await expect(recovered).resolves.toMatchObject({afterSequence: 0, options: undefined});
    await waitForSession(session, () => session.getState().status === 'connected');
    await session.close();
  });

  test('regression: a late start-up snapshot never overwrites a newer refresh snapshot', async () => {
    // The shell used to fold each loaded snapshot before the core checked its reply, so the
    // start-up snapshot, answered after an overlapping refresh's, rolled the run's status back.
    // Both are at one sequence, where the run fold itself cannot tell which is newer.
    const older = snapshotResponse({sequence: 1, status: 'running'});
    const newer = snapshotResponse({sequence: 1, status: 'paused'});
    for (const startupFirst of [false, true]) {
      const lifecycle = new FakeLifecycle();
      const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
      const startup = deferred<ProtocolResponse>();
      const refresh = deferred<ProtocolResponse>();
      transport.snapshots.push(startup.promise, refresh.promise);
      const started = session.start();
      lifecycle.emit('online');
      expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(
        2,
      );

      const status = () => session.store.getState().status;
      const answerStartup = async () => {
        startup.resolve(older);
        await started;
      };
      const answerRefresh = async () => {
        refresh.resolve(newer);
        await waitForSession(session, () => status() === 'paused');
      };
      // The refresh's stream retry is not part of this ordering assertion.
      // Keep its bootstrap silent so only the two fenced snapshot replies can
      // change the fold.
      transport.silentDials = 1;
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
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(2);
    expect(transport.reconnectCalls).toBe(1);

    // The lifecycle retry owns the one control redial and snapshot request,
    // but an explicit user request that arrives before that snapshot settles
    // must promote its eventual stream recovery to a fresh bootstrap.
    session.reattach();
    session.reattach();
    lifecycle.emit('online');
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(2);
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
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(2);

    const resumed = await retrying;
    expect(resumed).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});

    resumed.onMessage(outputBatch('store-1', 2, 'superseded retry\n'));
    expect(session.store.getState().sequence).toBe(1);
    expect(session.store.getState().transcript).toEqual([]);

    const fresh = transport.nextSubscription();
    transport.releasePendingSubscribe();
    await expect(fresh).resolves.toMatchObject({afterSequence: 0, options: undefined});
    await refreshed;

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

  test('queues one reattach behind a recovery cursor dial before its first batch', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.silentDials = 2;
    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    initial.onMessage(streamBatch('store-1', 1));
    await starting;

    transport.deferNextSubscribe();
    const retrying = transport.nextSubscription();
    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    expect(scheduler.runNext()).toBe(true);
    expect(await retrying).toMatchObject({
      afterSequence: 1,
      options: {storeId: 'store-1'},
    });

    session.reattach();
    session.reattach();
    expect(transport.subscriptions).toHaveLength(2);

    const fresh = transport.nextSubscription();
    transport.releasePendingSubscribe();
    await expect(fresh).resolves.toMatchObject({afterSequence: 0, options: undefined});

    expect(transport.subscriptions).toHaveLength(3);
    await session.close();
  });

  test('allows another Reattach after a fresh bootstrap fails before its first batch', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    await session.start();

    transport.silentDials = 2;
    const failedDial = transport.nextSubscription();
    session.reattach();
    const failed = await failedDial;
    failed.onDisconnect(disconnect('fresh bootstrap failed'));
    expect(session.getState()).toMatchObject({
      status: 'stale',
      error: {message: 'fresh bootstrap failed'},
    });

    const retriedDial = transport.nextSubscription();
    session.reattach();
    const retried = await retriedDial;
    expect(retried).toMatchObject({afterSequence: 0, options: undefined});
    expect(transport.subscriptions).toHaveLength(3);

    await session.close();
  });

  test('coalesces reattach with a pending initial stream bootstrap', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.deferNextSubscribe();
    const initialSubscription = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialSubscription;

    // start() has completed its fenced snapshot but still owns the first
    // sequence-zero handshake. Reattach is the same fresh intent, so it must not create a
    // second socket whose handle could later be orphaned by the first one.
    session.reattach();
    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(1);

    transport.releasePendingSubscribe();
    await starting;

    // The already-delivered initial generation must retire the first reattach
    // owner. A later request therefore starts its own fresh subscription rather
    // than remaining stranded behind a transition that can never complete.
    transport.silentDials = 1;
    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(2);

    await session.close();
    expect(initial.closed).toBe(true);
    expect(transport.subscriptions.every(subscription => subscription.closed)).toBe(true);
  });

  test('retires recovery when its replacement generation completes startup', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.silentDials = 2;
    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    await settle();

    initial.onDisconnect(disconnect('lost before the first batch'));
    expect(session.getState().status).toBe('stale');

    const replacementDial = transport.nextSubscription();
    session.reattach();
    const replacement = await replacementDial;
    replacement.onMessage(streamBatch('replacement-store', 1));
    await starting;
    await settle();
    expect(session.getState().status).toBe('connected');

    // The replacement generation satisfied both startup and the overlapping
    // recovery. A later explicit recovery must not be suppressed by a stale
    // recovery owner left in the core.
    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(3);
    await session.close();
  });

  test('retries a failed startup replacement whose subscription handle is pending', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.silentDials = 2;
    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    await settle();

    initial.onDisconnect(disconnect('lost before the first batch'));
    const failure = new Error('replacement snapshot failed');
    transport.snapshots.push(failure);
    transport.deferNextSubscribe();
    const replacementDial = transport.nextSubscription();
    session.reattach();
    const replacement = await replacementDial;

    // The replacement batch is observable before subscribe() returns its
    // handle. Its fenced snapshot fails while that fresh dial still owns the
    // stream.
    replacement.onMessage(streamBatch('replacement-store', 1));
    await waitForSession(session, () => session.getState().error === failure);
    await starting;

    // Reattach coalesces with that already-established generation, but still
    // owns a new fenced snapshot. Adopting the late handle cannot erase the
    // first failure while the replacement proof is pending.
    const retrySnapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(retrySnapshot.promise);
    session.reattach();
    await settle();
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(2);
    expect(
      transport.requests.filter(request => request.type === 'query.snapshot').at(-1),
    ).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'replacement-store',
    });
    transport.releasePendingSubscribe();
    await settle();
    expect(session.getState()).toMatchObject({status: 'stale', error: failure});

    const recovered = waitForSession(session, () => session.getState().status === 'connected');
    retrySnapshot.resolve(snapshotResponse({sequence: 1}));
    await recovered;

    // Both the startup and the retry owner have settled, so a later explicit
    // Reattach can establish another fresh generation.
    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(3);
    await session.close();
  });

  test('accepts a successful recovery snapshot before its pending handle is adopted', async () => {
    const connection = new PendingHandleConnection();
    const session = new WebSession({
      lifecycle: new FakeLifecycle(),
      connect: () => connection,
    });
    const starting = session.start();
    await waitForSession(session, () => session.getState().error === connection.failure);
    session.reattach();
    await waitForSession(session, () => session.getState().hasSnapshot);
    expect(session.getState()).toMatchObject({status: 'stale', error: connection.failure});

    // The successful fenced retry is authoritative, but only adopting the same stream generation
    // supplies the connection proof needed to clear the earlier snapshot failure.
    connection.release();
    await starting;
    expect(session.getState()).toMatchObject({status: 'connected', error: null});
    await session.close();
  });

  test('does not let a snapshot success erase a newer stream fault', async () => {
    const connection = new FaultBeforeSnapshotConnection();
    const session = new WebSession({
      lifecycle: new FakeLifecycle(),
      connect: () => connection,
    });
    await session.start();
    expect(session.getState()).toMatchObject({status: 'stale', error: connection.firstFailure});

    session.reattach();
    await connection.rebootstrapStarted;
    connection.establishFaultedReplacement();
    expect(session.getState()).toMatchObject({status: 'stale', error: connection.streamFault});

    const applied = waitForSession(session, () => session.getState().hasSnapshot);
    connection.resolveReplacementSnapshot();
    await applied;

    expect(session.getState()).toMatchObject({status: 'stale', error: connection.streamFault});
    await session.close();
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
    expect(transport.requests.filter(request => request.type === 'query.snapshot').at(-1)).toEqual({
      type: 'query.snapshot',
      expected_store_id: '',
    });
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
    expect(transport.requests.filter(request => request.type === 'query.snapshot')).toHaveLength(2);

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
    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'completed'}));

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

function iterationResponse(revision: string, label: string, runId = 'run-1'): ProtocolResponse {
  const ordinal = Number(label.split(' ').at(-1));
  return {
    protocol_version: 1,
    request_id: `iterations-${revision}`,
    timestamp: '2026-01-01T00:00:00Z',
    ok: true,
    iterations_ready: true,
    iterations: {
      kind: 'page',
      schema_version: 1,
      run_id: runId,
      projection_revision: revision,
      parent_id: null,
      from_cursor: null,
      items: [
        {
          id: `generation:${ordinal}`,
          parent_id: null,
          kind: 'generation',
          label,
          ordinal,
          status: 'completed',
          child_count: 0,
          headline_metrics: [],
        },
      ],
      next_cursor: null,
    },
  };
}

function iterationPageResponse(
  revision: string,
  fromCursor: string | null,
  start: number,
  count: number,
  nextCursor: string | null,
): ProtocolResponse {
  return {
    protocol_version: 1,
    request_id: `iterations-${revision}-${fromCursor ?? 'first'}`,
    timestamp: '2026-01-01T00:00:00Z',
    ok: true,
    iterations_ready: true,
    iterations: {
      kind: 'page',
      schema_version: 1,
      run_id: 'run-1',
      projection_revision: revision,
      parent_id: null,
      from_cursor: fromCursor,
      items: Array.from({length: count}, (_, offset) => {
        const ordinal = start + offset;
        return {
          id: `generation:${ordinal}`,
          parent_id: null,
          kind: 'generation' as const,
          label: `Generation ${ordinal}`,
          ordinal,
          status: 'completed' as const,
          child_count: 0,
          headline_metrics: [],
        };
      }),
      next_cursor: nextCursor,
    },
  };
}

function iterationStoreReplacedError(): ServerError {
  return new ServerError('The run event store changed before iterations were captured', {
    code: 'iteration_store_replaced',
    summary: 'The run event store changed before iterations were captured',
    scope: 'request',
  });
}

/** The typed server refusal for an expected-store snapshot fence. */
function storeReplacedResponse(): ProtocolResponse {
  return {
    ok: false,
    request_id: 'request-store-replaced',
    timestamp: '2026-01-01T00:00:00Z',
    error: 'Request failed',
    diagnostic: {
      code: 'snapshot_store_replaced',
      summary: 'Request failed',
      scope: 'request',
    },
  };
}

/** Resolves after the session core next publishes a distinct state. */
function nextSessionChange(session: WebSession): Promise<void> {
  return new Promise(resolve => {
    const unsubscribe = session.subscribe(() => {
      unsubscribe();
      resolve();
    });
  });
}

/** Let the transport's and the session's promise callbacks run: microtasks only, no timer. */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 50; turn += 1) await Promise.resolve();
}

describe('trace publications', () => {
  test('a page hears accepted trace data in order after the authoritative fold', async () => {
    const heard: Array<{type: string; status: string}> = [];
    const session: WebSession = new WebSession({
      lifecycle: new FakeLifecycle(),
      connect: connectRun(
        hooks => {
          const transport = new FakeTransport(hooks);
          transport.snapshots.push(snapshotResponse({sequence: 1, status: 'running'}));
          return transport;
        },
        {reconnectDelaysMs: [0]},
      ),
      onTracePublication: publication =>
        heard.push({type: publication.kind, status: session.store.getState().status}),
    });
    await session.start();
    await settle();
    expect(heard).toContainEqual({type: 'snapshot', status: 'running'});
    await session.close();
  });

  test('publishes a delayed historical snapshot even after the event cursor raced ahead', async () => {
    const loaded = deferred<ProtocolResponse>();
    const publications: RunTracePublication[] = [];
    const built: FakeTransport[] = [];
    const session = new WebSession({
      lifecycle: new FakeLifecycle(),
      connect: connectRun(
        hooks => {
          const transport = new FakeTransport(hooks);
          transport.snapshots.push(loaded.promise);
          built.push(transport);
          return transport;
        },
        {reconnectDelaysMs: [0]},
      ),
      onTracePublication: publication => publications.push(publication),
    });
    const transport = built[0];
    if (transport === undefined) throw new Error('session did not build its transport');

    const next = transport.nextSubscription();
    const started = session.start();
    const subscription = await next;
    subscription.onMessage(
      eventBatch([spanStarted(2, 'newer-span')], {
        store_id: 'store-1',
        through_sequence: 2,
        history_after_sequence: 0,
      }),
    );
    loaded.resolve(snapshotResponse({sequence: 1, spans: [traceSpan('stale-snapshot-only')]}));
    await started;
    await settle();

    expect(session.store.getState().sequence).toBe(2);
    expect(publications.filter(publication => publication.kind === 'snapshot')).toEqual([
      {
        kind: 'snapshot',
        snapshot: expect.objectContaining({
          sequence: 1,
          spans: [expect.objectContaining({id: 'stale-snapshot-only'})],
        }),
      },
    ]);
    expect(
      publications.some(
        publication =>
          publication.kind === 'events' && publication.events.some(item => item.sequence === 2),
      ),
    ).toBe(true);
    await session.close();
  });

  test('publishes the accepted subset of a mixed-run batch and reports its rejection', async () => {
    const publications: RunTracePublication[] = [];
    const built: FakeTransport[] = [];
    const session = new WebSession({
      lifecycle: new FakeLifecycle(),
      connect: connectRun(
        hooks => {
          const transport = new FakeTransport(hooks);
          transport.snapshots.push(snapshotResponse({run_id: 'run-a', sequence: 1}));
          built.push(transport);
          return transport;
        },
        {reconnectDelaysMs: [0]},
      ),
      onTracePublication: publication => publications.push(publication),
    });
    const transport = built[0];
    if (transport === undefined) throw new Error('session did not build its transport');
    await session.start();

    transport.subscriptions[0]?.onMessage(
      eventBatch(
        [
          event(2, 'span_started', {
            run_id: 'run-a',
            data: {kind: 'span_started', span: traceSpan('accepted-run-a')},
          }),
          event(3, 'span_started', {
            run_id: 'run-b',
            data: {kind: 'span_started', span: traceSpan('rejected-run-b')},
          }),
        ],
        {store_id: 'store-1', through_sequence: 3, history_after_sequence: 0},
      ),
    );
    await settle();

    expect(session.store.getState()).toMatchObject({runId: 'run-a', sequence: 2});
    expect(
      publications
        .filter(publication => publication.kind === 'events')
        .flatMap(publication => publication.events)
        .filter(item => (item.sequence ?? 0) >= 2)
        .map(item => ({runId: item.run_id, sequence: item.sequence})),
    ).toEqual([{runId: 'run-a', sequence: 2}]);
    expect(session.getState().error?.message).toContain('run-b');
    await session.close();
  });

  test('publishes whether a rebootstrap keeps or replaces the established stream store', async () => {
    const publications: RunTracePublication[] = [];
    const built: FakeTransport[] = [];
    const session = new WebSession({
      lifecycle: new FakeLifecycle(),
      connect: connectRun(
        hooks => {
          const transport = new FakeTransport(hooks);
          transport.snapshots.push(snapshotResponse({sequence: 1}));
          built.push(transport);
          return transport;
        },
        {reconnectDelaysMs: [0]},
      ),
      onTracePublication: publication => publications.push(publication),
    });
    const transport = built[0];
    if (transport === undefined) throw new Error('session did not build its transport');
    await session.start();
    const subscription = transport.subscriptions[0];
    if (subscription === undefined) throw new Error('initial subscription did not open');

    subscription.onMessage(
      eventBatch([spanStarted(2, 'same-store')], {
        store_id: 'store-1',
        through_sequence: 2,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    transport.snapshots.push(snapshotResponse({sequence: 1}));
    subscription.onMessage(
      eventBatch([spanStarted(1, 'replacement-store')], {
        store_id: 'store-2',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await settle();

    expect(
      publications
        .filter(publication => publication.kind === 'rebootstrap')
        .map(publication => publication.base),
    ).toEqual(['preserve', 'reset']);
    await session.close();
  });

  test('loads a fenced snapshot after a same-store rebootstrap cannot prove run identity', async () => {
    const publications: RunTracePublication[] = [];
    const built: FakeTransport[] = [];
    const session = new WebSession({
      lifecycle: new FakeLifecycle(),
      connect: connectRun(
        hooks => {
          const transport = new FakeTransport(hooks);
          transport.snapshots.push(
            snapshotResponse({sequence: 1, spans: [traceSpan('old-snapshot-span')]}),
          );
          built.push(transport);
          return transport;
        },
        {reconnectDelaysMs: [0]},
      ),
      onTracePublication: publication => publications.push(publication),
    });
    const transport = built[0];
    if (transport === undefined) throw new Error('session did not build its transport');
    await session.start();
    transport.snapshots.push(
      snapshotResponse({
        sequence: 2,
        spans: [traceSpan('old-snapshot-span'), traceSpan('identity-unknown')],
      }),
    );

    transport.subscriptions[0]?.onMessage(
      eventBatch(
        [
          event(2, 'span_started', {
            data: {kind: 'span_started', span: traceSpan('identity-unknown')},
          }),
        ],
        {
          store_id: 'store-1',
          through_sequence: 2,
          history_after_sequence: 0,
          rebootstrap: true,
        },
      ),
    );
    await settle();

    expect(
      transport.requests.filter(
        request => request.type === 'query.snapshot' && request.expected_store_id === 'store-1',
      ),
    ).toHaveLength(2);
    expect(publications.slice(-2)).toMatchObject([
      {kind: 'rebootstrap', base: 'reset', events: [{sequence: 2}]},
      {
        kind: 'snapshot',
        snapshot: {sequence: 2, spans: [{id: 'old-snapshot-span'}, {id: 'identity-unknown'}]},
      },
    ]);
    expect(session.store.getState().runId).toBe('run-1');
    expect(session.getState().error).toBeNull();
    await session.close();
  });
});
