import {describe, expect, it} from 'bun:test';
import type {EventSubscription} from './client.js';
import {
  PersistentEventStream,
  type PersistentEventStreamCallbacks,
  type StreamConnectionState,
  type StreamTransport,
} from './persistent-event-stream.js';
import type {RunEvent, ServerMessage} from './protocol.js';

/** Lets a zero-delay reconnect timer and its subscribe settle. */
const settle = () => new Promise<void>(resolve => setTimeout(resolve, 1));

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
 * A transport whose stream a test can sever, whose dials it can refuse, defer,
 * or arm to deliver a batch synchronously (before the subscribe promise
 * resolves, as the production client does when `subscribed` and the first batch
 * share one socket chunk). It records every subscription so a test can see
 * which were closed.
 */
class StubTransport implements StreamTransport {
  readonly subscribeCalls: Array<{
    afterSequence: number;
    tail: number | undefined;
    storeId: string | undefined;
  }> = [];
  readonly subscriptions: Array<{closed: boolean}> = [];
  /** How many upcoming subscribes to reject before letting one through. */
  refuseSubscribes = 0;
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
    if (this.refuseSubscribes > 0) {
      this.refuseSubscribes -= 1;
      return Promise.reject(new Error('connection refused'));
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
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
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
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
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
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);

    // The tail probe plus its full-replay fallback, then the failure reported.
    expect(transport.subscribeCalls).toHaveLength(2);
    expect(states.map(state => state.status)).toEqual(['disconnected']);

    // Only a stream that once connected can drop, so a boot failure is final.
    await settle();
    await settle();
    expect(transport.subscribeCalls).toHaveLength(2);
  });

  it('resumes from its own cursor, clears the outage, and resets the schedule', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, messages, states} = harness(env);
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([
      event(1, 'agent_output_chunk', 'one\n'),
      event(2, 'agent_output_chunk', 'two\n'),
    ]);
    env.cursor = 2;

    transport.sever();
    expect(states.map(state => state.status)).toEqual(['disconnected']);

    await settle();
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
    await settle();
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
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;
    // The first batch named a store; the caller now folds under it.
    env.storeId = 'run-store';

    transport.sever();
    await settle();

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
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;
    env.storeId = 'run-store';

    // An old server forbids `store_id` on subscribe, so the store-named dial is
    // rejected; the resume retries without it rather than failing the reconnect.
    transport.refuseSubscribes = 1;
    transport.sever();
    await settle();

    expect(transport.subscribeCalls.slice(1)).toEqual([
      {afterSequence: 1, tail: undefined, storeId: 'run-store'},
      {afterSequence: 1, tail: undefined, storeId: undefined},
    ]);
    expect(states.at(-1)?.status).toBe('connected');
  });

  it('stops dialing when the schedule runs out and stays disconnected', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, states} = harness(env);
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0, 0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;

    transport.refuseSubscribes = Number.POSITIVE_INFINITY;
    transport.sever();
    await settle();
    await settle();
    await settle();

    // The boot subscribe plus one attempt per schedule entry, then silence.
    expect(transport.subscribeCalls).toHaveLength(3);
    // Only the drop was announced; failed resume dials stay quiet.
    expect(states.map(state => state.status)).toEqual(['disconnected']);
  });

  it('does not reconnect when the caller declines it', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, states} = harness(env);
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'run_finished')]);
    env.cursor = 1;

    // A finished run has nothing more to stream, so the drop is not an outage.
    env.reconnect = false;
    transport.sever();
    await settle();
    expect(transport.subscribeCalls).toHaveLength(1);
    expect(states).toEqual([]);
  });

  it('does not reconnect once closed', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks} = harness(env);
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;

    await stream.close();
    transport.sever();
    await settle();
    expect(transport.subscribeCalls).toHaveLength(1);
  });

  it('tags the first resumed batch resumed even when it lands before subscribe resolves', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, messages} = harness(env);
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(6, 'agent_output_chunk', 'six\n')], 5);
    env.cursor = 6;

    transport.sever();
    // The resume subscribe delivers `subscribed` + `event_batch` in one chunk,
    // before its promise resolves: the flag would be unset if it were raised
    // only after the await.
    transport.deliverOnNextSubscribe([event(7, 'agent_output_chunk', 'seven\n')], 0);
    await settle();

    const resumed = messages.find(
      entry => entry.message.type === 'event_batch' && entry.message.events[0]?.sequence === 7,
    );
    expect(resumed?.resumed).toBe(true);
  });

  it('closes a reconnect subscription that resolves after close', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks} = harness(env);
    const stream = new PersistentEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;

    transport.deferNextSubscribe();
    transport.sever();
    await settle();
    // The boot subscribe plus the resume dial, which is now pending.
    expect(transport.subscribeCalls).toHaveLength(2);

    await stream.close();
    // close() cannot cancel a dial that has not resolved; let it land now.
    transport.releasePendingSubscribe();
    await settle();

    // The late subscription is closed, not adopted, so its socket cannot
    // outlive shutdown and deliver state after close.
    expect(transport.subscriptions[1]?.closed).toBe(true);
  });
});
