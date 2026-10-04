import {describe, it} from 'node:test';
import {BackendClientError, ServerError} from './errors.js';
import {
  PersistentEventStream,
  type PersistentEventStreamCallbacks,
  type PersistentEventStreamOptions,
  type StreamConnectionState,
  type StreamTransport,
} from './persistent-event-stream.js';
import type {RunEvent, ServerMessage} from './protocol.js';
import {expect} from './test-support/expect.js';
import {FakeClock} from './testing/fake-clock.test-helper.js';
import type {EventSubscription} from './transport.js';

/** A production stream with only its public scheduling seam replaced. */
class TestEventStream extends PersistentEventStream {
  readonly #scheduler: FakeClock;

  constructor(
    transport: StreamTransport,
    options: Omit<PersistentEventStreamOptions, 'scheduleTimeout'>,
  ) {
    const scheduler = new FakeClock();
    super(transport, {...options, scheduleTimeout: scheduler.schedule});
    this.#scheduler = scheduler;
  }

  /** Fire one pending reconnect, then drain the promise continuations it caused. */
  async settle(): Promise<void> {
    this.#scheduler.runOne();
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
  }
}

function event(sequence: number, type: RunEvent['type'], content?: string): RunEvent {
  return {
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type,
    ...(content === undefined
      ? {}
      : {data: {kind: 'agent_output_chunk', channel: 'assistant', content}}),
  };
}

/**
 * What one severed stream must report, by whether the bootstrap batch had
 * landed and whether the caller accepts a redial. A table rather than the
 * predicate restated: the point is which points of the space are silent, and
 * only one of the four is.
 */
const EXPECTED_REPORTS = {
  // Nothing folded and nothing coming: the fault is the whole transcript, so
  // saying nothing leaves an empty view reading as a complete one (#1044).
  'false/false': ['disconnected'],
  'false/true': ['disconnected', 'connected'],
  // The caller has the bootstrap and wants no redial, so the close took
  // nothing from it.
  'true/false': [],
  'true/true': ['disconnected', 'connected'],
} as const satisfies Record<string, readonly StreamConnectionState['status'][]>;

/** Mutable answers to the stream's `cursor`/`storeId`/`shouldReconnect` questions. */
interface Env {
  cursor: number;
  reconnect: boolean;
  storeId?: string;
}

function harness(env: Env): {
  callbacks: PersistentEventStreamCallbacks;
  messages: Array<{message: ServerMessage; resumed: boolean}>;
  states: StreamConnectionState[];
} {
  const messages: Array<{message: ServerMessage; resumed: boolean}> = [];
  const states: StreamConnectionState[] = [];
  return {
    messages,
    states,
    callbacks: {
      cursor: () => env.cursor,
      storeId: () => env.storeId ?? '',
      shouldReconnect: () => env.reconnect,
      onMessage: (message, {resumed}) => messages.push({message, resumed}),
      onConnectionState: state => states.push(state),
    },
  };
}

/**
 * A transport whose stream a test can sever, whose dials it can refuse or fail
 * with a scripted error, defer, or arm to deliver a batch synchronously (before
 * the subscribe promise resolves, as the production client does when
 * `subscribed` and the first batch share one socket chunk). It records every
 * subscription so a test can see which were closed.
 */
class StubTransport implements StreamTransport {
  readonly subscribeCalls: Array<{
    afterSequence: number;
    tail: number | undefined;
    storeId: string | undefined;
  }> = [];
  readonly subscriptions: Array<{closed: boolean}> = [];
  /**
   * How many upcoming subscribes the server refuses (a typed rejection, the
   * shape an old server produces for an unknown field) before one goes through.
   */
  refuseSubscribes = 0;
  /** Errors upcoming dials fail with, consumed in order before `refuseSubscribes`. */
  readonly scriptedDialFailures: Error[] = [];
  #message: ((message: ServerMessage) => void) | null = null;
  #disconnect: ((error: Error) => void) | null = null;
  #armed: {events: RunEvent[]; historyAfterSequence: number} | null = null;
  #deferNext = false;
  #release: (() => void) | null = null;

  /** Arm the next subscribe to deliver this batch before its promise resolves. */
  deliverOnNextSubscribe(events: readonly RunEvent[], historyAfterSequence: number): void {
    this.#armed = {events: [...events], historyAfterSequence};
  }

  /** Hold the next subscribe pending until `releasePendingSubscribe`. */
  deferNextSubscribe(): void {
    this.#deferNext = true;
  }

  releasePendingSubscribe(): void {
    const release = this.#release;
    this.#release = null;
    release?.();
  }

  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: {tail?: number; storeId?: string},
  ): Promise<EventSubscription> {
    this.subscribeCalls.push({afterSequence, tail: options?.tail, storeId: options?.storeId});
    const scripted = this.scriptedDialFailures.shift();
    if (scripted !== undefined) return Promise.reject(scripted);
    if (this.refuseSubscribes > 0) {
      this.refuseSubscribes -= 1;
      return Promise.reject(new ServerError('Extra inputs are not permitted'));
    }
    this.#message = onMessage;
    this.#disconnect = onDisconnect;
    const record = {closed: false};
    this.subscriptions.push(record);
    const subscription: EventSubscription = {
      close: async () => {
        record.closed = true;
      },
    };
    const armed = this.#armed;
    this.#armed = null;
    if (armed !== null) {
      onMessage({
        type: 'event_batch',
        events: armed.events,
        history_after_sequence: armed.historyAfterSequence,
      });
    }
    if (this.#deferNext) {
      this.#deferNext = false;
      return new Promise<EventSubscription>(resolve => {
        this.#release = () => resolve(subscription);
      });
    }
    return Promise.resolve(subscription);
  }

  emitBatch(events: readonly RunEvent[], historyAfterSequence = 0): void {
    this.#message?.({
      type: 'event_batch',
      events: [...events],
      history_after_sequence: historyAfterSequence,
    });
  }

  sever(message = 'Server event stream disconnected'): void {
    this.#disconnect?.(new Error(message));
  }
}

describe('PersistentEventStream', () => {
  it('boots with the tail, tags the bootstrap batch, and stays connected quietly', async () => {
    const transport = new StubTransport();
    const {callbacks, messages, states} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);

    expect(transport.subscribeCalls).toEqual([{afterSequence: 0, tail: 1_000, storeId: undefined}]);

    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    expect(messages).toHaveLength(1);
    expect(messages[0]?.resumed).toBe(false);
    // A successful boot changes nothing: the stream is connected by default.
    expect(states).toEqual([]);
  });

  it('falls back to a full replay when the server rejects the tail', async () => {
    const transport = new StubTransport();
    transport.refuseSubscribes = 1;
    const {callbacks, states} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);

    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 0, tail: undefined, storeId: undefined},
    ]);
    // The fallback succeeded, so no banner: the probe rejection is expected.
    expect(states).toEqual([]);
  });

  it('reports a boot failure and does not arm the reconnect loop', async () => {
    const transport = new StubTransport();
    transport.refuseSubscribes = Number.POSITIVE_INFINITY;
    const {callbacks, states} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);

    // The tail probe plus its full-replay fallback, then the failure reported.
    expect(transport.subscribeCalls).toHaveLength(2);
    expect(states.map(state => state.status)).toEqual(['disconnected']);

    // Only a stream that once connected can drop, so a boot failure is final.
    await stream.settle();
    await stream.settle();
    expect(transport.subscribeCalls).toHaveLength(2);
  });

  it('resumes from its own cursor, clears the outage, and resets the schedule', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, messages, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([
      event(1, 'agent_output_chunk', 'one\n'),
      event(2, 'agent_output_chunk', 'two\n'),
    ]);
    env.cursor = 2;

    transport.sever();
    expect(states.map(state => state.status)).toEqual(['disconnected']);

    await stream.settle();
    // The resume asks for events after the last one folded, with no tail. The
    // caller has seen no store, so the resume names none either.
    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 2, tail: undefined, storeId: undefined},
    ]);
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);

    // The resumed stream's batches are tagged resumed.
    transport.emitBatch([event(3, 'agent_output_chunk', 'three\n')]);
    expect(messages[messages.length - 1]?.resumed).toBe(true);

    // A success gives the next outage the full schedule again.
    env.cursor = 3;
    transport.sever();
    await stream.settle();
    expect(transport.subscribeCalls).toHaveLength(3);
    expect(transport.subscribeCalls[2]).toEqual({
      afterSequence: 3,
      tail: undefined,
      storeId: undefined,
    });
    expect(states[states.length - 1]?.status).toBe('connected');
  });

  it('names the store the caller last saw on a resume', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true, storeId: ''};
    const {callbacks} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;
    // The first batch named a store; the caller now folds under it.
    env.storeId = 'run-store';

    transport.sever();
    await stream.settle();

    // The resume carries that store so the server can drop the cursor if the
    // log was swapped while the stream was down.
    expect(transport.subscribeCalls.at(-1)).toEqual({
      afterSequence: 1,
      tail: undefined,
      storeId: 'run-store',
    });
  });

  it('falls back to a plain resume when the server rejects the store field', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true, storeId: ''};
    const {callbacks, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;
    env.storeId = 'run-store';

    // An old server forbids `store_id` on subscribe, so the store-named dial is
    // rejected; the resume retries without it rather than failing the reconnect.
    transport.refuseSubscribes = 1;
    transport.sever();
    await stream.settle();

    expect(transport.subscribeCalls.slice(1)).toEqual([
      {afterSequence: 1, tail: undefined, storeId: 'run-store'},
      {afterSequence: 1, tail: undefined, storeId: undefined},
    ]);
    expect(states.at(-1)?.status).toBe('connected');
  });

  it('keeps the store name when a transport failure interrupts the store probe', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true, storeId: ''};
    const {callbacks, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0, 0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;
    env.storeId = 'run-store';

    // A transient dial failure (the server is mid-restart) is not the server
    // refusing `store_id`. Downgrading to a cursor-only resume on it would let
    // a store swapped during the outage accept the stale cursor, so the next
    // attempt must carry the store name again instead.
    transport.scriptedDialFailures.push(
      new BackendClientError('disconnected', 'connection refused'),
    );
    transport.sever();
    await stream.settle();
    await stream.settle();

    expect(transport.subscribeCalls.slice(1)).toEqual([
      {afterSequence: 1, tail: undefined, storeId: 'run-store'},
      {afterSequence: 1, tail: undefined, storeId: 'run-store'},
    ]);
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);
  });

  it('reports a transport failure during the tail probe instead of downgrading the boot', async () => {
    const transport = new StubTransport();
    const {callbacks, states} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    transport.scriptedDialFailures.push(
      new BackendClientError('disconnected', 'connection refused'),
    );
    await stream.subscribe(callbacks);

    // One dial: a transport failure is not the server refusing `tail`, so the
    // boot does not retry a full replay against the same fault.
    expect(transport.subscribeCalls).toEqual([{afterSequence: 0, tail: 1_000, storeId: undefined}]);
    expect(states.map(state => state.status)).toEqual(['disconnected']);
    const failure = states[0]?.status === 'disconnected' ? states[0].error : undefined;
    expect(failure).toBeInstanceOf(BackendClientError);
    expect(failure).toMatchObject({kind: 'disconnected', message: 'connection refused'});
  });

  it('stops dialing when the schedule runs out and stays disconnected', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0, 0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;

    transport.refuseSubscribes = Number.POSITIVE_INFINITY;
    transport.sever();
    await stream.settle();
    await stream.settle();
    await stream.settle();

    // The boot subscribe plus one attempt per schedule entry, then silence.
    expect(transport.subscribeCalls).toHaveLength(3);
    // Only the drop was announced; failed resume dials stay quiet.
    expect(states.map(state => state.status)).toEqual(['disconnected']);
  });

  it('does not reconnect when the caller declines it', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'run_finished')]);
    env.cursor = 1;

    // A finished run has nothing more to stream, and the bootstrap already
    // landed, so the socket closing behind it says nothing the caller does not
    // have: not an outage, and not worth reporting.
    env.reconnect = false;
    transport.sever();
    await stream.settle();
    expect(transport.subscribeCalls).toHaveLength(1);
    expect(states).toEqual([]);
  });

  it('reports a drop before the first batch on a run it will not redial', async () => {
    const transport = new StubTransport();
    // Terminal from the caller's first look, which is what a run reopened after
    // it finished gives: its snapshot carries the status and no events, so the
    // predicate is already false when the socket faults.
    const {callbacks, states} = harness({cursor: 0, reconnect: false});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);

    // A frame the client rejected, arriving between `subscribed` and the
    // bootstrap batch. Nothing has folded, so this report is the whole
    // difference between an empty transcript and an empty transcript that says
    // it is empty (#1044).
    transport.sever('Invalid event batch message');
    await stream.settle();

    expect(states.map(state => state.status)).toEqual(['disconnected']);
    const reported = states[0];
    expect(reported?.status === 'disconnected' ? reported.error.message : null).toBe(
      'Invalid event batch message',
    );
    // Reported, not redialed: the redial policy for an ended run is unchanged.
    expect(transport.subscribeCalls).toHaveLength(1);
    await stream.close();
  });

  it('reports every drop that cost the caller something, and redials separately', async () => {
    // The disconnect path decides two things, so the property is their cross
    // product. The redial is the caller's call. The report is not: it is
    // withheld only where the caller has the bootstrap and wants no redial,
    // which is the one combination where the drop took nothing from it.
    // Conflating the two is how a declined pre-bootstrap drop went unreported
    // and cost the whole transcript (#1044).
    for (const bootstrapped of [false, true]) {
      for (const reconnect of [false, true]) {
        const where = {bootstrapped, reconnect};
        const transport = new StubTransport();
        const env = {cursor: 0, reconnect};
        const {callbacks, states} = harness(env);
        const stream = new TestEventStream(transport, {
          tail: 1_000,
          reconnectDelaysMs: [0],
        });
        await stream.subscribe(callbacks);
        if (bootstrapped) {
          transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
          env.cursor = 1;
        }

        transport.sever();
        await stream.settle();

        expect({...where, states: states.map(state => state.status)}).toEqual({
          ...where,
          states: [...EXPECTED_REPORTS[`${bootstrapped}/${reconnect}`]],
        });
        // One dial per accepted redial, and none for a declined one. The
        // bootstrapped case resumes; the other re-bootstraps.
        expect({...where, dials: transport.subscribeCalls.length}).toEqual({
          ...where,
          dials: reconnect ? 2 : 1,
        });
        await stream.close();
      }
    }
  });

  it('does not reconnect once closed', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;

    await stream.close();
    transport.sever();
    await stream.settle();
    expect(transport.subscribeCalls).toHaveLength(1);
  });

  it('tags the first resumed batch resumed even when it lands before subscribe resolves', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, messages} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(6, 'agent_output_chunk', 'six\n')], 5);
    env.cursor = 6;

    transport.sever();
    // The resume subscribe delivers `subscribed` + `event_batch` in one chunk,
    // before its promise resolves: the flag would be unset if it were raised
    // only after the await.
    transport.deliverOnNextSubscribe([event(7, 'agent_output_chunk', 'seven\n')], 0);
    await stream.settle();

    const resumed = messages.find(
      entry => entry.message.type === 'event_batch' && entry.message.events[0]?.sequence === 7,
    );
    expect(resumed?.resumed).toBe(true);
  });

  it('closes a reconnect subscription that resolves after close', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;

    transport.deferNextSubscribe();
    transport.sever();
    await stream.settle();
    // The boot subscribe plus the resume dial, which is now pending.
    expect(transport.subscribeCalls).toHaveLength(2);

    await stream.close();
    // close() cannot cancel a dial that has not resolved; let it land now.
    transport.releasePendingSubscribe();
    await stream.settle();

    // The late subscription is closed, not adopted, so its socket cannot
    // outlive shutdown and deliver state after close.
    expect(transport.subscriptions[1]?.closed).toBe(true);
  });

  it('reports a pre-bootstrap outage once across its retries', async () => {
    const transport = new StubTransport();
    const {callbacks, states} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0, 0]});
    await stream.subscribe(callbacks);
    // The boot connected but delivered no batch, so a drop re-bootstraps rather
    // than resuming, and every failed re-bootstrap used to re-report the outage.
    transport.scriptedDialFailures.push(
      new BackendClientError('disconnected', 'down'),
      new BackendClientError('disconnected', 'down'),
    );
    transport.sever();
    await stream.settle();
    await stream.settle();
    await stream.settle();

    // One disconnect for the whole outage: the initial drop and the two failed
    // re-bootstraps report a single transition, not one per attempt.
    expect(states.map(state => state.status)).toEqual(['disconnected']);
    // The finite schedule ran to exhaustion: the boot dial plus two retries.
    expect(transport.subscribeCalls).toHaveLength(3);
    await stream.close();
  });

  it('redials and recovers when retry() is called after the schedule is exhausted', async () => {
    const transport = new StubTransport();
    const {callbacks, states} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.scriptedDialFailures.push(new BackendClientError('disconnected', 'down'));
    transport.sever();
    await stream.settle();
    await stream.settle();
    // The single-entry schedule is spent; the stream is down with the disconnect
    // standing and no timer pending.
    expect(states.map(state => state.status)).toEqual(['disconnected']);
    expect(transport.subscribeCalls).toHaveLength(2);

    // The server is back; the caller redials on demand and recovers.
    stream.retry();
    await stream.settle();
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);
    expect(transport.subscribeCalls).toHaveLength(3);

    // Recovery cleared the outage flag, so the next drop reports afresh.
    transport.sever();
    expect(states.map(state => state.status)).toEqual([
      'disconnected',
      'connected',
      'disconnected',
    ]);
    await stream.close();
  });

  it('retry() does not stack a redial while a reconnect is already pending', async () => {
    const transport = new StubTransport();
    const {callbacks} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [50]});
    await stream.subscribe(callbacks);
    transport.scriptedDialFailures.push(new BackendClientError('disconnected', 'down'));
    transport.sever();

    // A reconnect is scheduled 50ms out. retry() must defer to it, not fire a
    // second dial now.
    stream.retry();
    stream.retry();
    expect(transport.subscribeCalls).toHaveLength(1);
    await stream.close();
  });

  it('retry() stays down for a drop the caller deems not worth reconnecting', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: false};
    const {callbacks, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);

    stream.retry();
    await stream.settle();
    // shouldReconnect() is false (a finished run), so retry() is a no-op: no
    // extra dial, no state churn.
    expect(transport.subscribeCalls).toHaveLength(1);
    expect(states).toEqual([]);
    await stream.close();
  });
});
