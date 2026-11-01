/** Browser fixture for the same protobuf port ABI the Electron preload exposes. */

import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  encodeUpdate,
  type GatewayBridge,
  GatewayError,
  type GatewayPortBridge,
  gatewayEnd,
  gatewayReply,
  parseGatewayRequest,
  parseSubscriptionCall,
} from '@vibesys/ipc';
import {
  DrainGoal,
  HostCallRequestSchema,
  type Record as LogRecord,
  RecordSchema,
  type RunEntry,
  RunEntrySchema,
  RunLiveness,
  SkeletonResponseSchema,
  TailRequestSchema,
  TailResponseSchema,
  TailStreamRequestSchema,
  TailStreamResponseSchema,
  TopicEntrySchema,
} from '@vibesys/proto';
import {foldSkeleton, observeRun, seedSkeleton} from '@vibesys/run-skeleton';
import {ChatPeer} from './chat-peer.js';
import type {WireFixture} from './gateway-fixture.js';

type Receive = (bytes: Uint8Array) => void;

const settled = (bytes: Uint8Array) => ({result: Promise.resolve(bytes), cancel: () => undefined});

/** The page-side gateway: serves the fixture's frames and records what the client asked of it. */
class FixtureGateway {
  readonly calls: string[] = [];
  readonly streams = new Set<Receive>();
  readonly ended = new Set<(error?: string) => void>();
  /** The windows' observations of the run's liveness, through the subscription port. */
  readonly runObservers = new Set<Receive>();
  readonly history: number[][];
  readonly held = new Set<Receive>();
  readonly logs = new Map<string, {data: Uint8Array; subscribers: Map<Receive, number>}>();
  readonly observations: ReturnType<typeof fixtureObservations>;
  readonly chat: ChatPeer;
  active = 0;
  currentObservation: number[];

  constructor(readonly input: WireFixture) {
    this.history = [input.tail];
    this.currentObservation = input.run;
    this.observations = fixtureObservations(input);
    this.chat = new ChatPeer(input.chat);
    for (const [id, bytes] of Object.entries(input.streams ?? {}))
      this.logs.set(id, {
        data: fromBinary(TailStreamResponseSchema, new Uint8Array(bytes)).data,
        subscribers: new Map(),
      });
  }

  unary = (host: string, method: string, request: Uint8Array) => {
    this.calls.push(method);
    const local = this.chat.unary(method, request);
    if (local) return settled(local);
    const refused = this.input.behavior?.skeleton;
    if (method === this.input.methods.skeleton && refused)
      return {
        result: Promise.reject(new GatewayError(refused.message, refused.status)),
        cancel: () => undefined,
      };
    if (method === this.input.methods.hostCall) return this.hostCall(host, request);
    const bytes =
      method === this.input.methods.skeleton
        ? this.input.skeleton
        : method === this.input.methods.blobs
          ? this.input.blobs[[...request].join(',')]
          : undefined;
    if (!bytes) throw new Error('Unknown unary');
    return settled(new Uint8Array(bytes));
  };

  stream = (
    _host: string,
    method: string,
    bytes: Uint8Array,
    receive: Receive,
    finish: (error?: string) => void,
  ) => {
    this.ended.add(finish);
    this.calls.push(method);
    this.active += 1;
    let closed = false;
    const cancelChat = this.subscribe(method, bytes, receive);
    return {
      cancel: () => {
        if (closed) return;
        closed = true;
        this.active -= 1;
        this.streams.delete(receive);
        this.held.delete(receive);
        this.ended.delete(finish);
        cancelChat();
      },
    };
  };

  private hostCall(host: string, request: Uint8Array) {
    const peer = window as unknown as {
      postIntent(host: string, bytes: number[]): Promise<number[]>;
    };
    const operation = fromBinary(HostCallRequestSchema, request).method;
    if (
      operation.case === 'postIntent' &&
      this.input.behavior?.refuseActions?.includes(operation.value.intent.case ?? '')
    )
      return {
        result: Promise.reject(new Error('The run did not take the command')),
        cancel: () => undefined,
      };
    if (!peer.postIntent && operation.case === 'chatMessage')
      return settled(
        this.chat.message(
          operation.value.sessionId,
          operation.value.requestId,
          operation.value.text,
        ),
      );
    return {
      result: peer.postIntent(host, [...request]).then(bytes => new Uint8Array(bytes)),
      cancel: () => undefined,
    };
  }

  /** Subscribe one stream and return what releases it from the chat peer. */
  private subscribe(method: string, bytes: Uint8Array, receive: Receive): () => void {
    const {methods} = this.input;
    if (method === methods.tail) {
      const tail = fromBinary(TailRequestSchema, bytes);
      if (tail.sessionId !== '') return this.chat.watch(tail.sessionId, tail.fromSeq, receive);
      this.streams.add(receive);
      if (this.input.behavior?.holdTail) this.held.add(receive);
      else for (const entry of this.history) receive(new Uint8Array(entry));
    } else if (method === methods.log) return this.subscribeLog(bytes, receive);
    else throw new Error('Unknown stream');
    return () => undefined;
  }

  private subscribeLog(bytes: Uint8Array, receive: Receive): () => void {
    const request = fromBinary(TailStreamRequestSchema, bytes);
    const log = this.logs.get(request.streamId);
    if (!log) throw new Error(`Unknown stream: ${request.streamId}`);
    const offset = Number(request.fromOffset);
    if (offset > log.data.length) throw new Error('Stream offset is beyond its committed end');
    receive(
      toBinary(
        TailStreamResponseSchema,
        create(TailStreamResponseSchema, {
          data: log.data.slice(offset),
          nextOffset: BigInt(log.data.length),
        }),
      ),
    );
    log.subscribers.set(receive, log.data.length);
    return () => {
      log.subscribers.delete(receive);
    };
  }

  /** Whether the host is delivering. A host that speaks again after a drop is delivering. */
  private delivering = true;

  /** The host says what it says of the run, to every observation, as main relays a live stream. */
  private say(): void {
    this.delivering = true;
    for (const receive of this.runObservers) this.sendRun(receive);
  }

  private sendRun(receive: Receive): void {
    const entry = this.observations.decode(new Uint8Array(this.currentObservation));
    const updates = [
      {kind: 'begin' as const},
      {
        kind: 'entry' as const,
        entry: create(TopicEntrySchema, {
          key: this.input.runId,
          value: {case: 'runs', value: fromBinary(RunEntrySchema, entry)},
        }),
      },
      {kind: 'synced' as const},
      {kind: 'control' as const, control: {kind: 'live' as const, since: Date.now()}},
    ];
    for (const update of updates) receive(gatewayReply(encodeUpdate(update)));
  }

  /** The host's stream was lost: main's lease says so, and the window keeps what it had. */
  private lose(): void {
    this.delivering = false;
    for (const receive of this.runObservers)
      receive(
        gatewayReply(
          encodeUpdate({
            kind: 'control',
            control: {kind: 'stale', reason: 'silent', attempt: 1, valueAt: Date.now()},
          }),
        ),
      );
  }

  /** The window's port to main for the host's live data: here, only this run's liveness. */
  subscriptions(): GatewayPortBridge {
    return {
      open: (encoded, receive) => {
        const call = parseSubscriptionCall(encoded);
        if (call.kind !== 'observe' || call.request.topic.case !== 'runs') {
          receive(gatewayEnd('this world serves only the runs of its run'));
          return {cancel: () => undefined};
        }
        this.runObservers.add(receive);
        if (this.delivering) this.sendRun(receive);
        return {cancel: () => void this.runObservers.delete(receive)};
      },
    };
  }

  controls() {
    return {
      releaseTail: () => {
        for (const receive of this.held)
          for (const entry of this.history) receive(new Uint8Array(entry));
        this.held.clear();
      },
      appendStreams: (streams: Record<string, number[]>) => {
        for (const [id, bytes] of Object.entries(streams)) {
          const log = this.logs.get(id) ?? {
            data: new Uint8Array(),
            subscribers: new Map<Receive, number>(),
          };
          log.data = new Uint8Array([...log.data, ...bytes]);
          this.logs.set(id, log);
          for (const [receive, offset] of log.subscribers) {
            receive(
              toBinary(
                TailStreamResponseSchema,
                create(TailStreamResponseSchema, {
                  data: log.data.slice(offset),
                  nextOffset: BigInt(log.data.length),
                }),
              ),
            );
            log.subscribers.set(receive, log.data.length);
          }
        }
      },
      appendChat: (bytes: number[]) => {
        this.chat.append(bytes);
      },
      sendChatFrame: (bytes: number[]) => this.chat.publish(bytes),
      seedChat: (id: string, bytes: number[]) => this.chat.append(bytes, id),
      disconnect: () => {
        for (const finish of [...this.ended]) finish('SSH connection closed');
        this.lose();
      },
      calls: () => this.calls,
      active: () => this.active,
      observe: (bytes: number[]) => {
        this.currentObservation = bytes;
        this.say();
      },
      append: (bytes: number[]) => {
        this.observations.append(bytes);
        this.history.push(bytes);
        const response = fromBinary(RunEntrySchema, new Uint8Array(this.currentObservation));
        const current = this.observations.current();
        if (response.head) {
          response.head.epoch = current.epoch;
          response.head.lifecycle = current.lifecycle;
          response.head.committedSeq = current.throughSeq;
        }
        if (current.lifecycle?.state.case === 'ended') response.liveness = RunLiveness.ENDED;
        this.currentObservation = [...toBinary(RunEntrySchema, response)];
        this.say();
        for (const receive of this.streams) receive(new Uint8Array(bytes));
      },
    };
  }
}

/** What a journey reaches as `window.fakeGateway`: what the Fake gateway lets a test do to the run it serves. */
export type FakeGateway = ReturnType<FixtureGateway['controls']>;

export function installGateway(input: WireFixture): void {
  const gateway = new FixtureGateway(input);
  Object.assign(window, {
    vibesysGateway: preloadOver({unary: gateway.unary, stream: gateway.stream}),
    vibesysSubscription: gateway.subscriptions(),
    fakeGateway: gateway.controls(),
  });
}

function preloadOver(bridge: GatewayBridge): GatewayPortBridge {
  return {
    open(encoded, receive, failed) {
      const request = parseGatewayRequest(encoded);
      if (request.kind === 'stream') {
        return bridge.stream(
          request.host,
          request.method,
          request.bytes,
          bytes => receive(gatewayReply(bytes)),
          (error, status) => receive(gatewayEnd(error, status)),
        );
      }
      const call = bridge.unary(request.host, request.method, request.bytes);
      let cancelled = false;
      void call.result.then(
        bytes => {
          if (cancelled) return;
          receive(gatewayReply(bytes));
          if (!cancelled) receive(gatewayEnd());
        },
        error => {
          if (cancelled) return;
          if (error instanceof GatewayError) receive(gatewayEnd(error.message, error.status));
          else failed(String(error));
        },
      );
      return {
        cancel() {
          cancelled = true;
          call.cancel();
        },
      };
    },
  };
}

type Head = NonNullable<RunEntry['head']>;

/**
 * The gateway never states a head that is not whole: a spec's hint names the facts it is about,
 * and the rest is what the log says, as the gateway projects it. A process identity is never a
 * source of epoch authority, including in a Fake.
 */
function completeHead(head: Head, records: readonly LogRecord[]): void {
  if (head.epoch === 0) {
    const prefix = records.filter(
      record =>
        head.committedSeq === undefined ||
        (head.committedSeq > 0n && record.seq <= head.committedSeq),
    );
    head.epoch = observeRun(foldSkeleton(seedSkeleton(), prefix)).epoch;
  }
  if (head.strategy === '') head.strategy = 'fixture';
  if (head.process !== undefined) {
    if (head.process.pid === 0) head.process.pid = 1;
    head.process.startedAt ??= timestampFromMs(1_700_000_000_000);
  }
  if (head.lifecycle === undefined && !head.awaitingLifecycle)
    head.lifecycle = observeRun(foldSkeleton(seedSkeleton(), records)).lifecycle;
  // A drain a spec's log records without saying what it drains toward is a pause, the first goal;
  // a head names its goal.
  const state = head.lifecycle?.state;
  if (state?.case === 'draining' && state.value.goal === DrainGoal.UNSPECIFIED)
    state.value.goal = DrainGoal.PARK;
}

function fixtureObservations(input: WireFixture) {
  const records = [
    ...fromBinary(SkeletonResponseSchema, new Uint8Array(input.skeleton)).records,
    ...fromBinary(TailResponseSchema, new Uint8Array(input.tail)).records,
  ].map(bytes => fromBinary(RecordSchema, bytes));
  const decode = (bytes: Uint8Array): Uint8Array => {
    const response = fromBinary(RunEntrySchema, bytes);
    if (response.head) completeHead(response.head, records);
    return toBinary(RunEntrySchema, response);
  };
  return {
    decode,
    current: () => {
      const skeleton = foldSkeleton(seedSkeleton(), records);
      return {...observeRun(skeleton), throughSeq: skeleton.throughSeq ?? 0n};
    },
    append(bytes: number[]): void {
      records.push(
        ...fromBinary(TailResponseSchema, new Uint8Array(bytes)).records.map(record =>
          fromBinary(RecordSchema, record),
        ),
      );
    },
  };
}
