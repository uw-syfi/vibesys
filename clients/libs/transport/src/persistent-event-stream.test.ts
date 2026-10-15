import {createHash} from 'node:crypto';
import {describe, it} from 'node:test';
import type {
  ActiveExecutionsChunkMessage,
  EventBatchMessage,
  RunEvent,
  ServerMessage,
} from '@vibesys/contracts';
import {BackendClientError, ServerError} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {
  PersistentEventStream,
  type PersistentEventStreamCallbacks,
  type PersistentEventStreamOptions,
  type StreamConnectionState,
  type StreamMessageContext,
  type StreamTransport,
} from './persistent-event-stream.js';
import {expect} from './test-support/expect.js';
import {FakeClock} from './testing/index.js';
import type {EventSubscription} from './transport.js';

function checkpointId(payload: string): string {
  return createHash('sha256').update(payload, 'utf8').digest('hex');
}

function deferredCheckpoint(
  id: string,
  throughSequence = 1,
  overrides: Partial<
    Omit<
      Extract<EventBatchMessage, {active_executions: null}>,
      'type' | 'active_executions' | 'active_executions_checkpoint_id'
    >
  > = {},
): Extract<EventBatchMessage, {active_executions: null}> {
  return {
    type: 'event_batch',
    events: [],
    active_executions: null,
    active_executions_checkpoint_id: id,
    through_sequence: throughSequence,
    store_id: 'store',
    history_after_sequence: 0,
    ...overrides,
  };
}

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
    this.#scheduler.runNext();
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
  }

  pendingDelays(): number[] {
    return this.#scheduler.pendingDelays();
  }
}

/**
 * What one severed stream must report, by whether the bootstrap batch had
 * landed and whether the caller accepts a redial. A table rather than the
 * predicate restated: the point is which points of the space are silent, and
 * only one of the eight is.
 */
const EXPECTED_REPORTS = {
  // Nothing folded and nothing coming: the fault is the whole transcript, so
  // saying nothing leaves an empty view reading as a complete one (#1044).
  'false/false/false': ['disconnected'],
  'false/false/true': ['disconnected'],
  'false/true/false': ['disconnected', 'connected'],
  'false/true/true': ['disconnected', 'connected'],
  // The caller has the bootstrap and wants no redial, so the close took
  // nothing from it unless the close carries a protocol fault of its own.
  'true/false/false': [],
  'true/false/true': ['disconnected'],
  'true/true/false': ['disconnected', 'connected'],
  'true/true/true': ['disconnected', 'connected'],
} as const satisfies Record<string, readonly StreamConnectionState['status'][]>;

/** Mutable answers to the stream's `cursor`/`storeId`/`shouldReconnect` questions. */
interface Env {
  cursor: number;
  reconnect: boolean;
  storeId?: string;
}

function harness(env: Env): {
  callbacks: PersistentEventStreamCallbacks;
  messages: Array<{message: ServerMessage} & StreamMessageContext>;
  states: StreamConnectionState[];
  nextState(): Promise<StreamConnectionState>;
} {
  const messages: Array<{message: ServerMessage} & StreamMessageContext> = [];
  const states: StreamConnectionState[] = [];
  const stateWaiters: Array<(state: StreamConnectionState) => void> = [];
  return {
    messages,
    states,
    nextState: () => new Promise(resolve => stateWaiters.push(resolve)),
    callbacks: {
      cursor: () => env.cursor,
      storeId: () => env.storeId ?? '',
      shouldReconnect: () => env.reconnect,
      onMessage: (message, context) => messages.push({message, ...context}),
      onConnectionState: state => {
        states.push(state);
        stateWaiters.shift()?.(state);
      },
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
  /** Whether a refused capability probe first emits its terminal protocol frame. */
  protocolErrorBeforeRefusal = false;
  /** Whether the next accepted dial emits a protocol fault and then one invalid later frame. */
  faultThenBatchOnNextSubscribe = false;
  /** Errors upcoming dials fail with, consumed in order before `refuseSubscribes`. */
  readonly scriptedDialFailures: Error[] = [];
  readonly scriptedCloseFailures: Error[] = [];
  readonly #subscribeWaiters: Array<
    (call: {afterSequence: number; tail: number | undefined; storeId: string | undefined}) => void
  > = [];
  #message: ((message: ServerMessage) => void) | null = null;
  #disconnect: ((error: Error) => void) | null = null;
  #armed: {events: RunEvent[]; historyAfterSequence: number} | null = null;
  #deferNext = false;
  #deferredFailure: Error | null = null;
  #release: (() => void) | null = null;
  #deferNextClose = false;
  #releaseClose: (() => void) | null = null;
  #nextCloseStarted: (() => void) | null = null;

  /** Arm the next subscribe to deliver this batch before its promise resolves. */
  deliverOnNextSubscribe(events: readonly RunEvent[], historyAfterSequence: number): void {
    this.#armed = {events: [...events], historyAfterSequence};
  }

  /** Hold the next subscribe pending until `releasePendingSubscribe`. */
  deferNextSubscribe(): void {
    this.#deferNext = true;
  }

  /** Hold one failed subscribe until `releasePendingSubscribe`. */
  deferNextSubscribeFailure(error: Error): void {
    this.#deferredFailure = error;
  }

  releasePendingSubscribe(): void {
    const release = this.#release;
    this.#release = null;
    release?.();
  }

  /** Hold the next accepted subscription's close until `releasePendingClose`. */
  deferNextSubscriptionClose(): void {
    this.#deferNextClose = true;
  }

  /** Invoke `listener` synchronously when the next accepted handle starts closing. */
  onNextSubscriptionClose(listener: () => void): void {
    this.#nextCloseStarted = listener;
  }

  releasePendingClose(): void {
    const release = this.#releaseClose;
    this.#releaseClose = null;
    release?.();
  }

  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: {tail?: number; storeId?: string},
  ): Promise<EventSubscription> {
    const call = {afterSequence, tail: options?.tail, storeId: options?.storeId};
    this.subscribeCalls.push(call);
    this.#subscribeWaiters.shift()?.(call);
    const deferredFailure = this.#deferredFailure;
    this.#deferredFailure = null;
    if (deferredFailure !== null) {
      return new Promise<EventSubscription>((_resolve, reject) => {
        this.#release = () => reject(deferredFailure);
      });
    }
    const scripted = this.scriptedDialFailures.shift();
    if (scripted !== undefined) return Promise.reject(scripted);
    if (this.refuseSubscribes > 0) {
      this.refuseSubscribes -= 1;
      if (this.protocolErrorBeforeRefusal) {
        onMessage({type: 'protocol_error', code: 'unsupported', message: 'Field unsupported'});
      }
      return Promise.reject(new ServerError('Extra inputs are not permitted'));
    }
    this.#message = onMessage;
    this.#disconnect = onDisconnect;
    const record = {closed: false};
    this.subscriptions.push(record);
    const closeSettled: Promise<void> | null = this.#deferNextClose
      ? new Promise<void>(resolve => {
          this.#releaseClose = resolve;
        })
      : null;
    const closeFailure = this.scriptedCloseFailures.shift();
    const closeStarted = this.#nextCloseStarted;
    this.#nextCloseStarted = null;
    this.#deferNextClose = false;
    let closing = false;
    const subscription: EventSubscription = {
      close: async () => {
        if (!closing) {
          closing = true;
          closeStarted?.();
        }
        record.closed = true;
        if (closeSettled !== null) await closeSettled;
        if (closeFailure !== undefined) throw closeFailure;
      },
    };
    const armed = this.#armed;
    this.#armed = null;
    if (this.faultThenBatchOnNextSubscribe) {
      this.faultThenBatchOnNextSubscribe = false;
      onMessage({type: 'protocol_error', code: 'stream_failed', message: 'Replay failed'});
      onMessage({type: 'event_batch', events: [], history_after_sequence: 0});
    }
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

  /** Resolve when the stream makes its next subscribe call. */
  nextSubscribe(): Promise<{
    afterSequence: number;
    tail: number | undefined;
    storeId: string | undefined;
  }> {
    return new Promise(resolve => this.#subscribeWaiters.push(resolve));
  }

  emitBatch(events: readonly RunEvent[], historyAfterSequence = 0, rebootstrap?: boolean): void {
    this.#message?.({
      type: 'event_batch',
      events: [...events],
      history_after_sequence: historyAfterSequence,
      ...(rebootstrap === undefined ? {} : {rebootstrap}),
    });
  }

  emit(message: ServerMessage): void {
    this.#message?.(message);
  }

  emitProtocolError(): void {
    this.#message?.({type: 'protocol_error', code: 'stream_failed', message: 'Replay failed'});
  }

  sever(error: Error | string = 'Server event stream disconnected'): void {
    this.#disconnect?.(typeof error === 'string' ? new Error(error) : error);
  }
}

/** Check one point in the report/redial/cause state space. */
async function checkDisconnectCase(
  bootstrapped: boolean,
  reconnect: boolean,
  protocolFault: boolean,
): Promise<void> {
  const where = {bootstrapped, reconnect, protocolFault};
  const transport = new StubTransport();
  const env = {cursor: 0, reconnect};
  const {callbacks, states} = harness(env);
  const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
  await stream.subscribe(callbacks);
  if (bootstrapped) {
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;
  }

  transport.sever(
    new BackendClientError(
      protocolFault ? 'parse' : 'disconnected',
      protocolFault ? 'Invalid event batch message' : 'Server event stream disconnected',
    ),
  );
  await stream.settle();

  expect({...where, states: states.map(state => state.status)}).toEqual({
    ...where,
    states: [...EXPECTED_REPORTS[`${bootstrapped}/${reconnect}/${protocolFault}`]],
  });
  // Cause changes whether an ended drop is reported, never whether it redials
  // on its own. A protocol fault waits for an explicit fresh bootstrap.
  expect({...where, dials: transport.subscribeCalls.length}).toEqual({
    ...where,
    dials: reconnect ? 2 : 1,
  });
  await stream.close();
}

describe('PersistentEventStream', () => {
  it('publishes a fragmented active-execution checkpoint only after its final chunk', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    const payload = JSON.stringify([
      {
        execution_id: 'new-execution',
        agent_kind: 'implementer',
        round_label: 'round-1',
        stage: 'implementer',
        attempt: null,
        assignment: 'new é水𝄞 assignment',
        started_at: '2026-01-01T00:00:00Z',
        activity: {
          kind: 'agent_execution_activity_changed',
          mode: 'thinking',
          summary: 'working',
          tool: null,
        },
        provider: null,
        model: null,
      },
    ]);
    const split = Math.floor(payload.length / 2);
    const id = checkpointId(payload);
    const chunk = (
      chunkIndex: number,
      part: string,
      chunkId = id,
    ): ActiveExecutionsChunkMessage => ({
      type: 'active_executions_chunk',
      checkpoint_id: chunkId,
      chunk_index: chunkIndex,
      chunk_count: 2,
      payload: part,
      through_sequence: 9,
      store_id: 'store',
      history_after_sequence: 3,
    });

    const firstReplay = event(8, 'agent_output_chunk', 'first replayed output');
    const secondReplay = event(9, 'agent_output_chunk', 'second replayed output');
    const replayEvents = [firstReplay, secondReplay];
    transport.emit(
      deferredCheckpoint(id, 8, {
        events: [firstReplay],
        history_after_sequence: 3,
        rebootstrap: true,
      }),
    );
    transport.emit(
      deferredCheckpoint(id, 9, {
        events: [secondReplay],
        history_after_sequence: 3,
      }),
    );
    transport.emit(chunk(0, payload.slice(0, split)));
    expect(messages).toHaveLength(2);
    expect(messages[0]?.message).toMatchObject({
      type: 'event_batch',
      active_executions: null,
      rebootstrap: true,
    });
    expect(messages[0]?.activeExecutionCheckpoint).toEqual({
      kind: 'deferred',
      checkpointId: id,
    });
    expect(messages[1]?.activeExecutionCheckpoint).toEqual({
      kind: 'deferred',
      checkpointId: id,
    });

    transport.emit(chunk(1, payload.slice(split)));
    expect(messages).toHaveLength(3);
    expect(messages[2]?.message).toMatchObject({
      type: 'event_batch',
      events: [],
      through_sequence: 9,
      store_id: 'store',
      history_after_sequence: 3,
      active_executions: [{execution_id: 'new-execution'}],
      rebootstrap: false,
    });
    expect(messages[2]?.activeExecutionCheckpoint).toEqual({
      kind: 'complete',
      checkpointId: id,
    });
    expect(
      messages.flatMap(({message}) => (message.type === 'event_batch' ? message.events : [])),
    ).toEqual(replayEvents);
    await stream.close();
  });

  it('does not retain or repeat many singleton frames before checkpoint completion', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    const checkpoint = '[]';
    const id = checkpointId(checkpoint);
    // Retaining these frames in the assembler would make an otherwise bounded
    // checkpoint transaction grow with every valid event frame.
    const frameCount = 50_000;

    for (let sequence = 1; sequence <= frameCount; sequence += 1) {
      transport.emit(
        deferredCheckpoint(id, sequence, {
          events: [event(sequence, 'agent_output_chunk')],
          rebootstrap: sequence === 1,
        }),
      );
    }
    transport.emit({
      type: 'active_executions_chunk',
      checkpoint_id: id,
      chunk_index: 0,
      chunk_count: 1,
      payload: checkpoint,
      through_sequence: frameCount,
      store_id: 'store',
      history_after_sequence: 0,
    });

    expect(messages).toHaveLength(frameCount + 1);
    const completed = messages.at(-1)?.message;
    if (completed?.type !== 'event_batch') throw new Error('Expected the completed checkpoint');
    expect(completed.events).toEqual([]);
    expect(completed.rebootstrap).toBe(false);
    await stream.close();
  });

  it('rejects a rebootstrap marker after the first deferred event frame', async () => {
    const transport = new StubTransport();
    const {callbacks} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    const id = checkpointId('[]');

    transport.emit(deferredCheckpoint(id, 1, {events: [event(1, 'agent_output_chunk')]}));
    expect(() =>
      transport.emit(
        deferredCheckpoint(id, 2, {
          events: [event(2, 'agent_output_chunk')],
          rebootstrap: true,
        }),
      ),
    ).toThrow('rebootstrap marker repeated after the first deferred event frame');
    await stream.close();
  });

  it('lets a new first chunk supersede an incomplete checkpoint without publishing a prefix', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    const first = (id: string, payload: string): ActiveExecutionsChunkMessage => ({
      type: 'active_executions_chunk',
      checkpoint_id: id,
      chunk_index: 0,
      chunk_count: 2,
      payload,
      through_sequence: 1,
      store_id: 'store',
      history_after_sequence: 0,
    });

    const abandonedId = 'a'.repeat(64);
    transport.emit(deferredCheckpoint(abandonedId));
    transport.emit(first(abandonedId, '[{"discarded":'));
    const replacementId = checkpointId('[]');
    transport.emit(deferredCheckpoint(replacementId));
    transport.emit(first(replacementId, '['));
    transport.emit({...first(replacementId, ']'), chunk_index: 1});

    expect(messages).toHaveLength(3);
    expect(messages[2]?.message).toMatchObject({type: 'event_batch', active_executions: []});
    await stream.close();
  });

  it('lets a new deferred checkpoint supersede one that received no fragment', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    const abandonedId = 'a'.repeat(64);
    const replacementId = checkpointId('[]');

    transport.emit(deferredCheckpoint(abandonedId, 1));
    transport.emit(deferredCheckpoint(replacementId, 2));
    transport.emit({
      type: 'active_executions_chunk',
      checkpoint_id: replacementId,
      chunk_index: 0,
      chunk_count: 1,
      payload: '[]',
      through_sequence: 2,
      store_id: 'store',
      history_after_sequence: 0,
    });

    expect(messages).toHaveLength(3);
    expect(messages[2]?.message).toMatchObject({
      type: 'event_batch',
      active_executions: [],
      through_sequence: 2,
    });
    await stream.close();
  });

  it('rejects a duplicate, gap, or metadata change instead of assembling mixed checkpoints', async () => {
    const cases = [
      {name: 'duplicate', secondIndex: 1, thirdIndex: 1, thirdStore: 'store'},
      {name: 'gap', secondIndex: 2, thirdIndex: 2, thirdStore: 'store'},
      {name: 'metadata', secondIndex: 1, thirdIndex: 2, thirdStore: 'other-store'},
    ] as const;
    for (const item of cases) {
      const transport = new StubTransport();
      const {callbacks, messages} = harness({cursor: 0, reconnect: true});
      const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
      await stream.subscribe(callbacks);
      const part = (chunkIndex: number, storeId = 'store'): ActiveExecutionsChunkMessage => ({
        type: 'active_executions_chunk',
        checkpoint_id: 'c'.repeat(64),
        chunk_index: chunkIndex,
        chunk_count: 3,
        payload: chunkIndex === 0 ? '[' : chunkIndex === 2 ? ']' : '',
        through_sequence: 1,
        store_id: storeId,
        history_after_sequence: 0,
      });
      transport.emit(deferredCheckpoint('c'.repeat(64)));
      transport.emit(part(0));
      if (item.name !== 'gap') transport.emit(part(item.secondIndex));

      expect(() => transport.emit(part(item.thirdIndex, item.thirdStore))).toThrow(
        'Invalid active-execution checkpoint',
      );
      expect(messages).toHaveLength(1);
      await stream.close();
    }
  });

  it('rejects completed content whose SHA-256 does not match its checkpoint identity', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);

    transport.emit(deferredCheckpoint(checkpointId('[ ]')));
    expect(() =>
      transport.emit({
        type: 'active_executions_chunk',
        checkpoint_id: checkpointId('[ ]'),
        chunk_index: 0,
        chunk_count: 1,
        payload: '[]',
        through_sequence: 1,
        store_id: 'store',
        history_after_sequence: 0,
      }),
    ).toThrow('checkpoint_id does not match the assembled UTF-8 payload');
    expect(messages).toHaveLength(1);
    await stream.close();
  });

  it('rejects an aggregate checkpoint above the staged UTF-8 byte bound', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    const payload = '𝄞'.repeat(64 * 1024);
    const part = (chunkIndex: number): ActiveExecutionsChunkMessage => ({
      type: 'active_executions_chunk',
      checkpoint_id: 'd'.repeat(64),
      chunk_index: chunkIndex,
      chunk_count: 256,
      payload,
      through_sequence: 1,
      store_id: 'store',
      history_after_sequence: 0,
    });
    transport.emit(deferredCheckpoint('d'.repeat(64)));
    for (let index = 0; index < 64; index += 1) transport.emit(part(index));

    expect(() => transport.emit(part(64))).toThrow(
      'assembled payload exceeds 16777216 UTF-8 bytes',
    );
    expect(messages).toHaveLength(1);
    await stream.close();
  });

  it('resumes after event frames when a fragmented checkpoint fails validation', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true, storeId: 'store'};
    const {callbacks, messages} = harness(env);
    const stream = new TestEventStream(transport, {reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emit({
      type: 'event_batch',
      events: [event(1, 'agent_output_chunk', 'delivered\n')],
      through_sequence: 1,
      active_executions: null,
      active_executions_checkpoint_id: 'e'.repeat(64),
      store_id: 'store',
      history_after_sequence: 0,
    });
    env.cursor = 1;
    transport.emit({
      type: 'active_executions_chunk',
      checkpoint_id: 'e'.repeat(64),
      chunk_index: 0,
      chunk_count: 2,
      payload: '[',
      through_sequence: 1,
      store_id: 'store',
      history_after_sequence: 0,
    });

    expect(() =>
      transport.emit({
        type: 'active_executions_chunk',
        checkpoint_id: 'e'.repeat(64),
        chunk_index: 1,
        chunk_count: 2,
        payload: ']',
        through_sequence: 1,
        store_id: 'store',
        history_after_sequence: 0,
      }),
    ).toThrow('checkpoint_id does not match the assembled UTF-8 payload');
    transport.sever(
      new BackendClientError('parse', 'Invalid active-execution checkpoint content digest'),
    );
    await stream.settle();
    expect(transport.subscribeCalls.at(-1)).toEqual({
      afterSequence: 1,
      tail: undefined,
      storeId: 'store',
    });
    const recovered = '[]';
    const recoveredId = checkpointId(recovered);
    transport.emit(deferredCheckpoint(recoveredId));
    transport.emit({
      type: 'active_executions_chunk',
      checkpoint_id: recoveredId,
      chunk_index: 0,
      chunk_count: 1,
      payload: recovered,
      through_sequence: 1,
      store_id: 'store',
      history_after_sequence: 0,
    });

    expect(messages).toHaveLength(3);
    expect(messages[2]?.message).toMatchObject({
      type: 'event_batch',
      events: [],
      through_sequence: 1,
      active_executions: [],
    });
    await stream.close();
  });

  it('rejects fragments that are not immediately owned by a deferred batch', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);

    expect(() =>
      transport.emit({
        type: 'active_executions_chunk',
        checkpoint_id: checkpointId('[]'),
        chunk_index: 0,
        chunk_count: 1,
        payload: '[]',
        through_sequence: 1,
        store_id: 'store',
        history_after_sequence: 0,
      }),
    ).toThrow('first chunk has no preceding deferred event batch');
    expect(messages).toEqual([]);
    await stream.close();
  });

  it('lets an ordinary authoritative checkpoint supersede an incomplete transfer', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    const abandonedId = checkpointId('[]');
    transport.emit(deferredCheckpoint(abandonedId));
    transport.emit({
      type: 'active_executions_chunk',
      checkpoint_id: abandonedId,
      chunk_index: 0,
      chunk_count: 2,
      payload: '[',
      through_sequence: 1,
      store_id: 'store',
      history_after_sequence: 0,
    });
    transport.emit({
      type: 'event_batch',
      events: [],
      active_executions: [],
      through_sequence: 2,
      store_id: 'store',
      history_after_sequence: 0,
    });

    expect(() =>
      transport.emit({
        type: 'active_executions_chunk',
        checkpoint_id: abandonedId,
        chunk_index: 1,
        chunk_count: 2,
        payload: ']',
        through_sequence: 1,
        store_id: 'store',
        history_after_sequence: 0,
      }),
    ).toThrow('chunk 1 has no first chunk');
    expect(messages).toHaveLength(2);
    expect(messages[1]?.message).toMatchObject({active_executions: [], through_sequence: 2});
    await stream.close();
  });

  it('fences late fragments with the newer deferred checkpoint identity', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    const abandonedId = 'a'.repeat(64);
    transport.emit(deferredCheckpoint(abandonedId));
    transport.emit({
      type: 'active_executions_chunk',
      checkpoint_id: abandonedId,
      chunk_index: 0,
      chunk_count: 2,
      payload: '[',
      through_sequence: 1,
      store_id: 'store',
      history_after_sequence: 0,
    });

    const replacementId = checkpointId('[]');
    transport.emit(deferredCheckpoint(replacementId, 2));
    expect(() =>
      transport.emit({
        type: 'active_executions_chunk',
        checkpoint_id: abandonedId,
        chunk_index: 1,
        chunk_count: 2,
        payload: ']',
        through_sequence: 1,
        store_id: 'store',
        history_after_sequence: 0,
      }),
    ).toThrow('chunk 1 has no first chunk');
    expect(() =>
      transport.emit({
        type: 'active_executions_chunk',
        checkpoint_id: abandonedId,
        chunk_index: 0,
        chunk_count: 2,
        payload: '[',
        through_sequence: 1,
        store_id: 'store',
        history_after_sequence: 0,
      }),
    ).toThrow('first chunk does not match its deferred event batch');
    transport.emit({
      type: 'active_executions_chunk',
      checkpoint_id: replacementId,
      chunk_index: 0,
      chunk_count: 1,
      payload: '[]',
      through_sequence: 2,
      store_id: 'store',
      history_after_sequence: 0,
    });

    expect(messages.at(-1)?.message).toMatchObject({
      active_executions: [],
      through_sequence: 2,
    });
    await stream.close();
  });

  it('rejects unrelated messages interleaved before a deferred checkpoint completes', async () => {
    const transport = new StubTransport();
    const {callbacks} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    transport.emit(deferredCheckpoint(checkpointId('[]')));

    expect(() => transport.emit({type: 'event', event: event(2, 'agent_output_chunk')})).toThrow(
      'event interrupted a deferred checkpoint transfer',
    );
    await stream.close();
  });

  it('hashes lone UTF-16 surrogates with standard UTF-8 replacement semantics', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    const payload = JSON.stringify([
      {
        execution_id: 'surrogate',
        agent_kind: 'implementer',
        round_label: 'round-1',
        stage: 'implementer',
        attempt: null,
        assignment: 'marker',
        started_at: '2026-01-01T00:00:00Z',
        activity: {
          kind: 'agent_execution_activity_changed',
          mode: 'thinking',
          summary: 'working',
          tool: null,
        },
        provider: null,
        model: null,
      },
    ]).replace('marker', '\ud800');
    const id = checkpointId(payload);
    transport.emit(deferredCheckpoint(id));
    transport.emit({
      type: 'active_executions_chunk',
      checkpoint_id: id,
      chunk_index: 0,
      chunk_count: 1,
      payload,
      through_sequence: 1,
      store_id: 'store',
      history_after_sequence: 0,
    });

    expect(messages.at(-1)?.message).toMatchObject({
      active_executions: [{execution_id: 'surrogate', assignment: '\ud800'}],
    });
    await stream.close();
  });

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

  it('adopts a successful tail fallback after the rejected probe emits its protocol frame', async () => {
    const transport = new StubTransport();
    transport.refuseSubscribes = 1;
    transport.protocolErrorBeforeRefusal = true;
    transport.deliverOnNextSubscribe([], 0);
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: []});

    await stream.subscribe(callbacks);

    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 0, tail: undefined, storeId: undefined},
    ]);
    expect(messages.map(({message}) => message.type)).toEqual(['protocol_error', 'event_batch']);
    expect(transport.subscriptions).toEqual([{closed: false}]);
    await stream.close();
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
    const bootstrapGeneration = messages.at(-1)?.connectionGeneration;
    if (bootstrapGeneration === undefined) throw new Error('Bootstrap batch was not delivered');
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
    transport.emitBatch([event(3, 'agent_output_chunk', 'three\n')], 0, false);
    expect(messages[messages.length - 1]?.resumed).toBe(true);
    expect(messages[messages.length - 1]?.connectionGeneration).toBeGreaterThan(
      bootstrapGeneration,
    );

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

  it('treats an in-loop rebootstrap on a resumed dial as a fresh batch', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, messages, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(6, 'agent_output_chunk', 'six\n')], 5);
    env.cursor = 6;

    transport.sever();
    await stream.settle();
    transport.emitBatch([event(7, 'agent_output_chunk', 'seven\n')]);
    const resumedGeneration = messages.at(-1)?.connectionGeneration;
    if (resumedGeneration === undefined) throw new Error('Resumed batch was not delivered');
    // The new socket resumed from cursor 6, but its already-open subscription
    // later overflowed its tail and sent a second bootstrap. This marker is
    // therefore the one batch that must not inherit the dial's resumed flag.
    transport.emitBatch([event(20, 'agent_output_chunk', 'twenty\n')], 19, true);

    expect(messages[messages.length - 1]).toMatchObject({
      resumed: false,
      connectionGeneration: resumedGeneration,
    });
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);

    // A fresh batch also establishes a bootstrap for the current stream, so a
    // later terminal close remains quiet rather than reporting an empty fold.
    env.reconnect = false;
    transport.sever();
    await stream.settle();
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);
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
    // rejected after its terminal protocol frame; the resume retries without
    // it rather than letting that retired probe poison the fallback handle.
    transport.refuseSubscribes = 1;
    transport.protocolErrorBeforeRefusal = true;
    transport.sever();
    await stream.settle();

    expect(transport.subscribeCalls.slice(1)).toEqual([
      {afterSequence: 1, tail: undefined, storeId: 'run-store'},
      {afterSequence: 1, tail: undefined, storeId: undefined},
    ]);
    expect(states.at(-1)?.status).toBe('connected');
    expect(transport.subscriptions[1]?.closed).toBe(false);
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

  it('reports every missing-history or protocol fault, and redials separately', async () => {
    // The disconnect path decides two things, so the property is their cross
    // product. The redial is the caller's call. The report is not: it is
    // withheld only where the caller has the bootstrap and wants no redial,
    // which is the one combination where the drop took nothing from it.
    // Conflating the two is how a declined pre-bootstrap drop went unreported
    // and cost the whole transcript (#1044).
    for (const bootstrapped of [false, true]) {
      for (const reconnect of [false, true]) {
        for (const protocolFault of [false, true]) {
          await checkDisconnectCase(bootstrapped, reconnect, protocolFault);
        }
      }
    }
  });

  it('re-bootstraps explicitly after an ended run reports a protocol fault', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: false};
    const {callbacks, nextState, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'run_finished')]);
    env.cursor = 1;

    transport.sever(new BackendClientError('parse', 'Invalid event batch message'));
    expect(states.map(state => state.status)).toEqual(['disconnected']);
    expect(transport.subscribeCalls).toHaveLength(1);

    const recovered = nextState();
    stream.rebootstrap();
    await expect(recovered).resolves.toMatchObject({status: 'connected'});
    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 0, tail: 1_000, storeId: undefined},
    ]);
    await stream.close();
  });

  it('queues one explicit re-bootstrap behind an in-flight resume', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, messages, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;

    transport.deferNextSubscribe();
    transport.sever();
    await stream.settle();
    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 1, tail: undefined, storeId: undefined},
    ]);

    // The resume owns the dial slot, but a later explicit request is stronger:
    // retain it once, let the owned dial settle, then retire that handle and
    // replace it with one fresh bootstrap.
    stream.rebootstrap();
    stream.rebootstrap();
    transport.emitBatch([event(2, 'agent_output_chunk', 'superseded\n')]);
    expect(messages).toHaveLength(1);
    expect(messages[0]?.resumed).toBe(false);
    transport.releasePendingSubscribe();
    await stream.settle();
    await stream.settle();

    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 1, tail: undefined, storeId: undefined},
      {afterSequence: 0, tail: 1_000, storeId: undefined},
    ]);
    expect(transport.subscriptions[1]?.closed).toBe(true);
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);
    await stream.close();
  });

  it('cancels the automatic retry before a disconnected callback re-bootstraps', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, nextState, states} = harness(env);
    let stream!: TestEventStream;
    stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [10]});
    const reentrantCallbacks: PersistentEventStreamCallbacks = {
      ...callbacks,
      onConnectionState: state => {
        callbacks.onConnectionState(state);
        if (state.status === 'disconnected') stream.rebootstrap();
      },
    };
    await stream.subscribe(reentrantCallbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;

    const disconnected = nextState();
    const recovered = nextState();
    const fresh = transport.nextSubscribe();
    transport.sever();

    await expect(disconnected).resolves.toMatchObject({status: 'disconnected'});
    await expect(fresh).resolves.toEqual({
      afterSequence: 0,
      tail: 1_000,
      storeId: undefined,
    });
    await expect(recovered).resolves.toMatchObject({status: 'connected'});
    expect(stream.pendingDelays()).toEqual([]);
    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 0, tail: 1_000, storeId: undefined},
    ]);
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);
    await stream.close();
  });

  it('does not downgrade a superseded named resume before the fresh pass', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true, storeId: ''};
    const {callbacks, nextState} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;
    env.storeId = 'run-store';

    transport.deferNextSubscribeFailure(new ServerError('Extra inputs are not permitted'));
    transport.sever();
    await stream.settle();
    stream.rebootstrap();

    const recovered = nextState();
    transport.releasePendingSubscribe();
    await expect(recovered).resolves.toMatchObject({status: 'connected'});
    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 1, tail: undefined, storeId: 'run-store'},
      {afterSequence: 0, tail: 1_000, storeId: undefined},
    ]);
    await stream.close();
  });

  it('coalesces re-bootstrap with a recovery that is already fresh', async () => {
    const transport = new StubTransport();
    const {callbacks, nextState, states} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);

    // No bootstrap batch landed, so recovering this drop already requires a
    // sequence-zero dial. An explicit fresh request cannot make it fresher.
    transport.deferNextSubscribe();
    transport.sever();
    await stream.settle();
    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 0, tail: 1_000, storeId: undefined},
    ]);

    const recovered = nextState();
    expect(stream.rebootstrap()).toBe('transitioning');
    expect(stream.rebootstrap()).toBe('transitioning');
    transport.releasePendingSubscribe();
    await expect(recovered).resolves.toMatchObject({status: 'connected'});

    expect(transport.subscribeCalls).toHaveLength(2);
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);
    await stream.close();
  });

  it('retains a fresh retry requested after the active fresh dial reports failure', async () => {
    const transport = new StubTransport();
    const {callbacks, nextState} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);

    transport.deferNextSubscribe();
    const failedDial = transport.nextSubscribe();
    expect(stream.rebootstrap()).toBe('transitioning');
    await expect(failedDial).resolves.toEqual({
      afterSequence: 0,
      tail: 1_000,
      storeId: undefined,
    });

    const failure = new BackendClientError('disconnected', 'fresh bootstrap failed');
    const reported = nextState();
    transport.sever(failure);
    await expect(reported).resolves.toEqual({status: 'disconnected', error: failure});

    // The handle is still settling, but its failure has already been exposed.
    // A later explicit request is a new attempt, not a duplicate of the failed one.
    const retriedDial = transport.nextSubscribe();
    expect(stream.rebootstrap()).toBe('transitioning');
    expect(stream.rebootstrap()).toBe('transitioning');
    expect(transport.subscribeCalls).toHaveLength(2);

    transport.releasePendingSubscribe();
    await expect(retriedDial).resolves.toEqual({
      afterSequence: 0,
      tail: 1_000,
      storeId: undefined,
    });
    await stream.settle();

    expect(transport.subscribeCalls).toHaveLength(3);
    expect(transport.subscriptions[1]?.closed).toBe(true);
    await stream.close();
  });

  it('retains a fresh retry after an in-band fault ends a pending fresh dial', async () => {
    const transport = new StubTransport();
    const {callbacks} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);

    transport.deferNextSubscribe();
    const failedDial = transport.nextSubscribe();
    expect(stream.rebootstrap()).toBe('transitioning');
    await failedDial;
    transport.emitProtocolError();

    const retriedDial = transport.nextSubscribe();
    expect(stream.rebootstrap()).toBe('transitioning');
    transport.releasePendingSubscribe();
    await expect(retriedDial).resolves.toEqual({
      afterSequence: 0,
      tail: 1_000,
      storeId: undefined,
    });
    await stream.close();
  });

  it('fences frames after a protocol fault from a pending fresh generation', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: false});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);

    transport.faultThenBatchOnNextSubscribe = true;
    expect(stream.rebootstrap()).toBe('transitioning');
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();

    expect(messages.map(({message}) => message.type)).toEqual(['event_batch', 'protocol_error']);
    expect(transport.subscriptions[1]?.closed).toBe(true);
    await stream.close();
  });

  it('awaits a synchronously faulted pending handle during stream teardown', async () => {
    const transport = new StubTransport();
    const {callbacks} = harness({cursor: 0, reconnect: false});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);

    transport.deferNextSubscriptionClose();
    transport.faultThenBatchOnNextSubscribe = true;
    const faultedDial = transport.nextSubscribe();
    expect(stream.rebootstrap()).toBe('transitioning');
    await faultedDial;

    let settled = false;
    const closing = stream.close().then(() => {
      settled = true;
    });
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
    expect(settled).toBe(false);

    transport.releasePendingClose();
    await closing;
    expect(transport.subscriptions[1]?.closed).toBe(true);
  });

  it('contains a pending faulted handle close failure during teardown', async () => {
    const transport = new StubTransport();
    const {callbacks, states} = harness({cursor: 0, reconnect: false});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);

    transport.scriptedCloseFailures.push(new Error('socket close failed'));
    transport.faultThenBatchOnNextSubscribe = true;
    const faultedDial = transport.nextSubscribe();
    expect(stream.rebootstrap()).toBe('transitioning');
    await faultedDial;

    await stream.close();
    expect(states).toEqual([]);
    expect(transport.subscriptions[1]?.closed).toBe(true);
  });

  it('retires a synchronously faulted pending handle before its queued replacement', async () => {
    const transport = new StubTransport();
    const {callbacks} = harness({cursor: 0, reconnect: false});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);

    transport.deferNextSubscriptionClose();
    transport.faultThenBatchOnNextSubscribe = true;
    const faultedDial = transport.nextSubscribe();
    expect(stream.rebootstrap()).toBe('transitioning');
    await faultedDial;

    const replacement = transport.nextSubscribe();
    expect(stream.rebootstrap()).toBe('transitioning');
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
    expect(transport.subscribeCalls).toHaveLength(2);

    transport.releasePendingClose();
    await expect(replacement).resolves.toEqual({
      afterSequence: 0,
      tail: undefined,
      storeId: undefined,
    });
    expect(transport.subscriptions[1]?.closed).toBe(true);
    expect(transport.subscribeCalls).toHaveLength(3);
    await stream.close();
  });

  it('awaits a protocol-failed live handle during stream teardown', async () => {
    const transport = new StubTransport();
    transport.deferNextSubscriptionClose();
    const {callbacks} = harness({cursor: 0, reconnect: false});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    transport.emitProtocolError();

    let settled = false;
    const closing = stream.close().then(() => {
      settled = true;
    });
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
    expect(settled).toBe(false);

    transport.releasePendingClose();
    await closing;
    expect(transport.subscriptions[0]?.closed).toBe(true);
  });

  it('retires a protocol-failed live handle before dialing its replacement', async () => {
    const transport = new StubTransport();
    transport.deferNextSubscriptionClose();
    const {callbacks} = harness({cursor: 0, reconnect: false});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    transport.emitProtocolError();

    const replacement = transport.nextSubscribe();
    expect(stream.rebootstrap()).toBe('transitioning');
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
    expect(transport.subscribeCalls).toHaveLength(1);

    transport.releasePendingClose();
    await expect(replacement).resolves.toEqual({
      afterSequence: 0,
      tail: undefined,
      storeId: undefined,
    });
    expect(transport.subscribeCalls).toHaveLength(2);
    await stream.close();
  });

  it('continues replacement after a protocol-failed handle rejects close', async () => {
    const transport = new StubTransport();
    transport.scriptedCloseFailures.push(new Error('socket close failed'));
    const {callbacks} = harness({cursor: 0, reconnect: false});
    const stream = new TestEventStream(transport, {reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    transport.emitProtocolError();

    const replacement = transport.nextSubscribe();
    expect(stream.rebootstrap()).toBe('transitioning');
    await expect(replacement).resolves.toEqual({
      afterSequence: 0,
      tail: undefined,
      storeId: undefined,
    });
    expect(transport.subscriptions[0]?.closed).toBe(true);
    await stream.close();
  });

  it('identifies a coalesced fresh recovery whose first batch already landed', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, messages, nextState} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(5, 'agent_output_chunk', 'old\n')]);
    env.cursor = 5;

    // The replacement delivers its first batch synchronously, but its
    // subscription handle remains pending. The batch, not handle settlement,
    // is the boundary visible to consumers.
    transport.deferNextSubscribe();
    transport.deliverOnNextSubscribe([event(1, 'run_finished')], 0);
    expect(stream.rebootstrap()).toBe('transitioning');
    expect(stream.rebootstrap()).toBe('transitioning');
    await stream.settle();
    expect(messages.at(-1)).toMatchObject({resumed: false});
    expect(stream.rebootstrap()).toBe('current');
    expect(stream.rebootstrap()).toBe('current');
    expect(transport.subscribeCalls).toHaveLength(2);

    const recovered = nextState();
    transport.releasePendingSubscribe();
    await expect(recovered).resolves.toMatchObject({status: 'connected'});
    await stream.close();
    expect(stream.rebootstrap()).toBe('inactive');
  });

  it('retains one cursor retry requested while a fresh handshake is still settling', async () => {
    const transport = new StubTransport();
    const env = {cursor: 5, reconnect: false, storeId: 'replacement'};
    const {callbacks, messages, nextState} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(5, 'agent_output_chunk', 'old\n')]);

    // The fresh generation has already delivered its first batch, but its
    // subscription handle is still pending. A snapshot may now prove that the
    // terminal batch stopped short of durable history and request a cursor
    // retry before the fresh recovery releases the single dial slot.
    transport.deferNextSubscribe();
    transport.deliverOnNextSubscribe([event(1, 'agent_output_chunk', 'replacement\n')], 0);
    expect(stream.rebootstrap()).toBe('transitioning');
    await stream.settle();
    expect(messages.at(-1)).toMatchObject({resumed: false});
    const terminalClose = new Error('replacement closed before the snapshot settled');
    transport.sever(terminalClose);
    env.reconnect = true;
    const reported = nextState();
    stream.retryIfDisconnected();
    stream.retryIfDisconnected();
    expect(transport.subscribeCalls).toHaveLength(2);

    const retainedRetry = transport.nextSubscribe();
    transport.releasePendingSubscribe();
    await expect(reported).resolves.toEqual({status: 'disconnected', error: terminalClose});
    expect(await retainedRetry).toEqual({
      afterSequence: 5,
      tail: undefined,
      storeId: 'replacement',
    });
    expect(transport.subscribeCalls).toHaveLength(3);
    await stream.close();
  });

  it('keeps recovery requests behind the initial bootstrap handshake', async () => {
    const transport = new StubTransport();
    const {callbacks, states} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    transport.deferNextSubscribe();

    const subscribing = stream.subscribe(callbacks);
    expect(transport.subscribeCalls).toEqual([{afterSequence: 0, tail: 1_000, storeId: undefined}]);

    // The pending attempt is already a fresh bootstrap. Neither the stronger
    // public recovery verb nor a lifecycle retry may open a competing socket.
    expect(stream.rebootstrap()).toBe('transitioning');
    expect(stream.rebootstrap()).toBe('transitioning');
    stream.retry();
    stream.retry();
    expect(transport.subscribeCalls).toHaveLength(1);

    transport.releasePendingSubscribe();
    await subscribing;
    await stream.settle();
    expect(transport.subscribeCalls).toHaveLength(1);
    await stream.close();
    expect(transport.subscriptions.every(subscription => subscription.closed)).toBe(true);
    expect(states).toEqual([]);
  });

  it('reports a delivered initial bootstrap as current while its handle is pending', async () => {
    const transport = new StubTransport();
    const {callbacks, messages} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    transport.deliverOnNextSubscribe([event(1, 'agent_output_chunk', 'initial\n')], 0);
    transport.deferNextSubscribe();

    const subscribing = stream.subscribe(callbacks);
    expect(messages).toHaveLength(1);

    // Delivery establishes the fresh generation even though the transport has
    // not returned its handle. A reattach must adopt that generation rather
    // than claim a transition whose queued intent initial success will discard.
    expect(stream.rebootstrap()).toBe('current');
    expect(stream.rebootstrap()).toBe('current');
    expect(transport.subscribeCalls).toHaveLength(1);

    transport.releasePendingSubscribe();
    await subscribing;
    await stream.settle();
    expect(transport.subscribeCalls).toHaveLength(1);
    await stream.close();
  });

  it('retains fresh recovery when a delivered initial bootstrap fails before its handle settles', async () => {
    const transport = new StubTransport();
    const {callbacks, states} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    transport.deliverOnNextSubscribe([event(1, 'agent_output_chunk', 'initial\n')], 0);
    transport.deferNextSubscribe();

    const subscribing = stream.subscribe(callbacks);
    transport.sever(new Error('initial stream failed after bootstrap'));

    // The batch alone would make this generation current, but its subsequent
    // disconnect has already fenced it. Keep the explicit fresh intent behind
    // the still-pending handle instead of stranding the page disconnected.
    expect(stream.rebootstrap()).toBe('transitioning');
    expect(stream.rebootstrap()).toBe('transitioning');
    expect(transport.subscribeCalls).toHaveLength(1);

    transport.releasePendingSubscribe();
    await subscribing;
    await stream.settle();
    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 0, tail: 1_000, storeId: undefined},
    ]);
    expect(transport.subscriptions[0]?.closed).toBe(true);
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);
    await stream.close();
  });

  it('drains one retained wake after the initial bootstrap handshake fails', async () => {
    const transport = new StubTransport();
    const initialFailure = new Error('initial handshake failed');
    transport.deferNextSubscribeFailure(initialFailure);
    const {callbacks, states} = harness({cursor: 7, reconnect: true, storeId: 'old-store'});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});

    const subscribing = stream.subscribe(callbacks);
    stream.retry();
    stream.retry();
    expect(transport.subscribeCalls).toHaveLength(1);

    transport.releasePendingSubscribe();
    await subscribing;
    await stream.settle();

    // No bootstrap batch landed, so the retained cursor retry must recover from
    // sequence zero, with the configured tail, rather than resume old-store.
    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 0, tail: 1_000, storeId: undefined},
    ]);
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);
    await stream.close();
  });

  it('drains one explicit fresh attach after the initial bootstrap handshake fails', async () => {
    const transport = new StubTransport();
    transport.deferNextSubscribeFailure(new Error('initial handshake failed'));
    const {callbacks, states} = harness({cursor: 0, reconnect: false});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});

    const subscribing = stream.subscribe(callbacks);
    expect(stream.rebootstrap()).toBe('transitioning');
    expect(stream.rebootstrap()).toBe('transitioning');
    expect(transport.subscribeCalls).toHaveLength(1);

    transport.releasePendingSubscribe();
    await subscribing;
    await stream.settle();

    // Explicit fresh recovery ignores automatic-redial policy, as it does
    // after every later outage.
    expect(transport.subscribeCalls).toEqual([
      {afterSequence: 0, tail: 1_000, storeId: undefined},
      {afterSequence: 0, tail: 1_000, storeId: undefined},
    ]);
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);
    await stream.close();
  });

  it('coalesces initial recovery intent and lets fresh attach dominate wake', async () => {
    const transport = new StubTransport();
    transport.deferNextSubscribeFailure(new Error('initial handshake failed'));
    const env = {cursor: 0, reconnect: true};
    const {callbacks} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});

    const subscribing = stream.subscribe(callbacks);
    stream.retry();
    stream.retry();
    expect(stream.rebootstrap()).toBe('transitioning');
    expect(stream.rebootstrap()).toBe('transitioning');
    // A later weaker request cannot downgrade the retained fresh attach.
    stream.retry();
    env.reconnect = false;

    transport.releasePendingSubscribe();
    await subscribing;
    await stream.settle();
    expect(transport.subscribeCalls).toHaveLength(2);
    await stream.settle();
    expect(transport.subscribeCalls).toHaveLength(2);
    await stream.close();
  });

  it('close discards recovery intent retained by the initial bootstrap', async () => {
    const transport = new StubTransport();
    transport.deferNextSubscribeFailure(new Error('initial handshake failed'));
    const {callbacks, states} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});

    const subscribing = stream.subscribe(callbacks);
    stream.retry();
    expect(stream.rebootstrap()).toBe('transitioning');
    const closing = stream.close();

    transport.releasePendingSubscribe();
    await Promise.all([subscribing, closing]);
    await stream.settle();
    expect(transport.subscribeCalls).toHaveLength(1);
    expect(states).toEqual([]);
    expect(stream.rebootstrap()).toBe('inactive');
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

  it('awaits and closes a reconnect subscription that resolves during close', async () => {
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

    let settled = false;
    const closing = stream.close().then(() => {
      settled = true;
    });
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
    expect(settled).toBe(false);

    // The transport has no handshake cancellation primitive, so shutdown owns
    // the pending dial until its late handle can be closed.
    transport.releasePendingSubscribe();
    await closing;

    // The handle is closed before shutdown settles, not adopted or abandoned.
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

  it('retry() replaces a pending timer with one immediate redial and never stacks it', async () => {
    const transport = new StubTransport();
    const {callbacks, nextState} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [50]});
    await stream.subscribe(callbacks);
    transport.deferNextSubscribe();
    transport.sever();

    // The timer represents a background tab whose reconnect callback was
    // coalesced indefinitely. A wake overrides it instead of depending on it.
    expect(stream.pendingDelays()).toEqual([50]);
    const redial = transport.nextSubscribe();
    stream.retry();
    expect(stream.pendingDelays()).toEqual([]);
    expect(await redial).toEqual({afterSequence: 0, tail: 1_000, storeId: undefined});

    // Keep that subscribe pending and ask twice more. The in-flight dial owns
    // the attempt, so overriding the timer never creates parallel sockets.
    stream.retry();
    expect(transport.subscribeCalls).toHaveLength(2);
    const recovered = nextState();
    transport.releasePendingSubscribe();
    await expect(recovered).resolves.toMatchObject({status: 'connected'});
    expect(transport.subscribeCalls).toHaveLength(2);
    await stream.close();
  });

  it('close joins a live subscription retirement already started by retry', async () => {
    const transport = new StubTransport();
    const {callbacks} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: []});
    transport.deferNextSubscriptionClose();
    await stream.subscribe(callbacks);

    stream.retry();
    let closeSettled = false;
    const closing = stream.close().then(() => {
      closeSettled = true;
    });
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
    try {
      expect(closeSettled).toBe(false);
      expect(transport.subscribeCalls).toHaveLength(1);
    } finally {
      transport.releasePendingClose();
      await closing;
      for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
      await stream.close();
    }
    expect(transport.subscribeCalls).toHaveLength(1);
  });

  it('close joins and contains failed retirement started by rebootstrap', async () => {
    const transport = new StubTransport();
    const {callbacks} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: []});
    transport.deferNextSubscriptionClose();
    transport.scriptedCloseFailures.push(new Error('retired handle close failed'));
    await stream.subscribe(callbacks);

    expect(stream.rebootstrap()).toBe('transitioning');
    let closeSettled = false;
    const closing = stream.close().then(() => {
      closeSettled = true;
    });
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
    try {
      expect(closeSettled).toBe(false);
      expect(transport.subscribeCalls).toHaveLength(1);
    } finally {
      transport.releasePendingClose();
      await closing;
      for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
      await stream.close();
    }
    expect(transport.subscribeCalls).toHaveLength(1);
  });

  it('publishes protocol-fault retirement before handle close can reenter shutdown', async () => {
    const transport = new StubTransport();
    const {callbacks} = harness({cursor: 0, reconnect: true});
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: []});
    transport.deferNextSubscriptionClose();
    let terminalClose: Promise<void> | null = null;
    transport.onNextSubscriptionClose(() => {
      terminalClose = stream.close();
    });
    await stream.subscribe(callbacks);

    transport.emitProtocolError();
    const closing = terminalClose as Promise<void> | null;
    if (closing === null) throw new Error('The handle did not re-enter terminal shutdown');
    let closeSettled = false;
    void closing.then(() => {
      closeSettled = true;
    });
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
    try {
      expect(closeSettled).toBe(false);
      expect(transport.subscribeCalls).toHaveLength(1);
    } finally {
      transport.releasePendingClose();
      await closing;
      await stream.close();
    }
    expect(closeSettled).toBe(true);
    expect(transport.subscriptions).toEqual([{closed: true}]);
  });

  it('reports a failed replacement dial when retry retires a healthy stream', async () => {
    const transport = new StubTransport();
    const env = {cursor: 0, reconnect: true};
    const {callbacks, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: []});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    env.cursor = 1;

    const failure = new BackendClientError('disconnected', 'replacement dial failed');
    transport.scriptedDialFailures.push(failure);
    stream.retry();
    await stream.settle();

    // A lifecycle wake can proactively replace a live stream. If that dial fails, the old socket
    // is already retired, so silence would leave the caller claiming it is still connected.
    expect(transport.subscribeCalls).toHaveLength(2);
    expect(states).toEqual([{status: 'disconnected', error: failure}]);
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

  it('retry() promotes a prior quiet close when the caller later requires recovery', async () => {
    const transport = new StubTransport();
    const env = {cursor: 1, reconnect: false};
    const {callbacks, states} = harness(env);
    const stream = new TestEventStream(transport, {tail: 1_000, reconnectDelaysMs: [0]});
    await stream.subscribe(callbacks);
    transport.emitBatch([event(1, 'run_finished')]);

    stream.retryIfDisconnected();
    expect(transport.subscribeCalls).toHaveLength(1);

    transport.sever();
    expect(states).toEqual([]);
    expect(transport.subscribeCalls).toHaveLength(1);

    // A concurrent control-plane answer now proves the terminal fold was
    // incomplete. The same public verb stays inert on healthy streams, but a
    // remembered quiet close becomes a reported recovery.
    env.reconnect = true;
    stream.retryIfDisconnected();
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();

    expect(transport.subscribeCalls).toHaveLength(2);
    expect(states.map(state => state.status)).toEqual(['disconnected', 'connected']);
    await stream.close();
  });
});
