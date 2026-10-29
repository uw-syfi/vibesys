/** Browser fixture for the same protobuf port ABI the Electron preload exposes. */

import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  type GatewayBridge,
  GatewayError,
  type GatewayPortBridge,
  gatewayEnd,
  gatewayReply,
  parseGatewayRequest,
} from '@vibesys/ipc';
import {
  HostCallRequestSchema,
  RecordSchema,
  RunLiveness,
  SkeletonResponseSchema,
  TailRequestSchema,
  TailResponseSchema,
  TailStreamRequestSchema,
  TailStreamResponseSchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {foldSkeleton, observeRun, seedSkeleton} from '@vibesys/run-skeleton';
import {ChatPeer} from './chat-peer.js';
import type {WireFixture} from './fake-gateway.js';

type Receive = (bytes: Uint8Array) => void;

const settled = (bytes: Uint8Array) => ({result: Promise.resolve(bytes), cancel: () => undefined});

/** The page-side gateway: serves the fixture's frames and records what the client asked of it. */
class FixtureGateway {
  readonly calls: string[] = [];
  readonly streams = new Set<Receive>();
  readonly ended = new Set<(error?: string) => void>();
  readonly watchers = new Set<Receive>();
  readonly history: number[][];
  readonly held = new Set<Receive>();
  readonly logs = new Map<string, {data: Uint8Array; subscribers: Map<Receive, number>}>();
  readonly observations: ReturnType<typeof fixtureObservations>;
  readonly chat: ChatPeer;
  active = 0;
  currentObservation: number[];

  constructor(readonly input: WireFixture) {
    this.history = [input.tail];
    this.currentObservation = input.watchRuns;
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
        this.watchers.delete(receive);
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
    } else if (method === methods.watchRuns) {
      this.watchers.add(receive);
      receive(this.observations.decode(new Uint8Array(this.currentObservation)));
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
      },
      calls: () => this.calls,
      active: () => this.active,
      observe: (bytes: number[]) => {
        this.currentObservation = bytes;
        for (const receive of this.watchers)
          receive(this.observations.decode(new Uint8Array(bytes)));
      },
      append: (bytes: number[]) => {
        this.observations.append(bytes);
        this.history.push(bytes);
        const response = fromBinary(
          WatchRunsResponseSchema,
          new Uint8Array(this.currentObservation),
        );
        const current = this.observations.current();
        if (response.head) {
          response.head.epoch = current.epoch;
          response.head.lifecycle = current.lifecycle;
          response.head.committedSeq = current.throughSeq;
        }
        if (current.lifecycle?.state.case === 'ended') response.liveness = RunLiveness.ENDED;
        this.currentObservation = [...toBinary(WatchRunsResponseSchema, response)];
        for (const receive of this.watchers) receive(new Uint8Array(this.currentObservation));
        for (const receive of this.streams) receive(new Uint8Array(bytes));
      },
    };
  }
}

export function installGateway(input: WireFixture): void {
  const gateway = new FixtureGateway(input);
  Object.assign(window, {
    vibesysGateway: preloadOver({unary: gateway.unary, stream: gateway.stream}),
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

function fixtureObservations(input: WireFixture) {
  const records = [
    ...fromBinary(SkeletonResponseSchema, new Uint8Array(input.skeleton)).records,
    ...fromBinary(TailResponseSchema, new Uint8Array(input.tail)).records,
  ].map(bytes => fromBinary(RecordSchema, bytes));
  // Match the gateway's retained-head decoder using canonical log facts.
  // A process identity is never a source of epoch authority, including in a Fake.
  const decode = (bytes: Uint8Array): Uint8Array => {
    const response = fromBinary(WatchRunsResponseSchema, bytes);
    const head = response.head;
    if (head && head.epoch === 0) {
      const prefix = records.filter(
        record =>
          head.committedSeq === undefined ||
          (head.committedSeq > 0n && record.seq <= head.committedSeq),
      );
      head.epoch = observeRun(foldSkeleton(seedSkeleton(), prefix)).epoch;
    }
    return toBinary(WatchRunsResponseSchema, response);
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
