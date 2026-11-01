/** A real in-process HTTP/2 peer over memory duplexes, with controllable Tail delivery. */
import {createServer, type ServerHttp2Session, type ServerHttp2Stream} from 'node:http2';
import {Duplex, PassThrough} from 'node:stream';
import {create, fromBinary, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {
  API_SCHEMA_HASH,
  HelloRequestSchema,
  HelloResponseSchema,
  type HostCallRequest,
  HostCallRequestSchema,
  HostService,
  type MutateRequest,
  MutateRequestSchema,
  type ReadRequest,
  ReadRequestSchema,
  type SubscribeRequest,
  SubscribeRequestSchema,
  SubscribeResponseSchema,
  TailRequestSchema,
  validatedFromBinary,
  type WatchRequest,
  WatchRequestSchema,
} from '@vibesys/proto';
import {SUBSCRIBE_METHOD} from '@vibesys/subscription';
import {GrpcError} from '@vibesys/transport';
import {
  type GatewayDialer,
  HELLO_METHOD,
  MUTATE_METHOD,
  READ_METHOD,
  TAIL_METHOD,
  WATCH_METHOD,
} from '../shell/gateway.js';
import {GrpcDecoder, grpcFrame} from '../shell/grpc.js';

export interface FakeTail {
  readonly fromSeq: bigint;
  readonly runId: string;
  send(bytes: Uint8Array): void;
  disconnect(): void;
  end(code?: number, detail?: string): void;
}

export interface FakeHostCall {
  readonly request: HostCallRequest;
  end(code?: number, detail?: string): void;
}

export interface FakeRead {
  readonly request: ReadRequest;
  /** Settles when the client has ended this call: read the answer, cancelled it, or closed. */
  readonly closed: Promise<void>;
  /** Answer with one message and a clean end, as the gateway does. */
  respond(bytes: Uint8Array): void;
  end(code?: number, detail?: string): void;
}

export interface FakeMutate {
  readonly request: MutateRequest;
  end(code?: number, detail?: string): void;
}

export interface FakeSubscribe {
  readonly request: SubscribeRequest;
  /** Settles when the client has ended this stream: cancelled it, run out of patience, or closed. */
  readonly closed: Promise<void>;
  send(frame: Frame): void;
  /** The connection drops under the stream, as an ssh link does. */
  disconnect(): void;
  end(code?: number, detail?: string): void;
}

/** A frame as a test writes it: the case and the value's fields. */
type Frame = NonNullable<MessageInitShape<typeof SubscribeResponseSchema>['frame']>;

export interface FakeWatch {
  readonly request: WatchRequest;
  /** Settles when the client has ended this stream: cancelled it, run out of patience, or closed. */
  readonly closed: Promise<void>;
  send(bytes: Uint8Array): void;
  end(code?: number, detail?: string): void;
}

export class FakeGateway implements GatewayDialer {
  readonly forces: boolean[] = [];
  schemaHash = API_SCHEMA_HASH;
  /** Peer fault input, like FakeTail.send: override the handshake bytes or status. */
  helloFault: Uint8Array | GrpcError | undefined;
  /** Methods the peer answers with a status and no message, like a gateway refusing a run. */
  readonly #refusals = new Map<string, number>();
  readonly #server = createServer();
  readonly #sessions = new Set<ServerHttp2Session>();
  readonly #tails: FakeTail[] = [];
  readonly #hostCalls: FakeHostCall[] = [];
  readonly #hostWaiters: Array<(call: FakeHostCall) => void> = [];
  readonly #mutations: FakeMutate[] = [];
  readonly #mutateWaiters: Array<(call: FakeMutate) => void> = [];
  #subscribed = 0;
  readonly #subscribes: FakeSubscribe[] = [];
  readonly #subscribeWaiters: Array<(subscribe: FakeSubscribe) => void> = [];
  readonly #watches: FakeWatch[] = [];
  readonly #watchWaiters: Array<(watch: FakeWatch) => void> = [];
  /** Every read the peer has received, in the order it arrived, whether or not it was taken. */
  readonly readsReceived: ReadRequest[] = [];
  readonly #reads: FakeRead[] = [];
  readonly #readWaiters: Array<(call: FakeRead) => void> = [];
  readonly #dialWaiters: Array<() => void> = [];
  #dialsTaken = 0;
  readonly #waiters: Array<(tail: FakeTail) => void> = [];

  constructor() {
    this.#server.on('session', session => {
      this.#sessions.add(session);
      session.on('error', () => {});
      session.once('close', () => this.#sessions.delete(session));
    });
    this.#server.on('stream', (stream, headers) => {
      stream.on('error', () => {});
      const decoder = new GrpcDecoder();
      const messages: Uint8Array[] = [];
      stream.on('data', (chunk: Buffer) => messages.push(...decoder.push(chunk)));
      stream.on('end', () => {
        // A client that reset the stream (a session closed while its Hello was in flight) is
        // owed no answer, as the real server owes none.
        if (stream.destroyed) return;
        decoder.finish();
        const method = headers[':path'];
        stream.respond(
          {':status': 200, 'content-type': 'application/grpc'},
          {waitForTrailers: true},
        );
        const refused = this.#refusals.get(String(method));
        if (method === HELLO_METHOD) {
          this.#hello(stream, messages[0] as Uint8Array);
        } else if (refused !== undefined) {
          end(stream, refused, 'refused by the fake gateway');
        } else if (method === TAIL_METHOD) {
          this.#tail(stream, messages[0] as Uint8Array);
        } else if (!this.#held(method, stream, messages[0] as Uint8Array)) {
          end(stream, 12, 'unknown method');
        }
      });
    });
  }

  #tail(stream: ServerHttp2Stream, bytes: Uint8Array): void {
    const request = fromBinary(TailRequestSchema, bytes);
    const session = stream.session;
    const tail: FakeTail = {
      fromSeq: request.fromSeq,
      runId: request.runId,
      send: bytes => {
        stream.write(grpcFrame(bytes));
      },
      disconnect: () => session?.destroy(),
      end: (code, detail) => end(stream, code, detail),
    };
    const waiter = this.#waiters.shift();
    if (waiter === undefined) this.#tails.push(tail);
    else waiter(tail);
  }

  #held(method: string | undefined, stream: ServerHttp2Stream, bytes: Uint8Array): boolean {
    if (method === `/${HostService.typeName}/${HostService.method.hostCall.name}`) {
      this.#hostCall(stream, bytes);
      return true;
    }
    if (method === MUTATE_METHOD) {
      this.#mutate(stream, bytes);
      return true;
    }
    if (method === READ_METHOD) {
      this.#read(stream, bytes);
      return true;
    }
    if (method === WATCH_METHOD) {
      this.#watch(stream, bytes);
      return true;
    }
    if (method === SUBSCRIBE_METHOD) {
      this.#subscribe(stream, bytes);
      return true;
    }
    return false;
  }

  #hostCall(stream: ServerHttp2Stream, bytes: Uint8Array): void {
    const call: FakeHostCall = {
      request: fromBinary(HostCallRequestSchema, bytes),
      end: (code, detail) => end(stream, code, detail),
    };
    const waiter = this.#hostWaiters.shift();
    if (waiter === undefined) this.#hostCalls.push(call);
    else waiter(call);
  }

  #mutate(stream: ServerHttp2Stream, bytes: Uint8Array): void {
    const call: FakeMutate = {
      request: fromBinary(MutateRequestSchema, bytes),
      end: (code, detail) => end(stream, code, detail),
    };
    const waiter = this.#mutateWaiters.shift();
    if (waiter === undefined) this.#mutations.push(call);
    else waiter(call);
  }

  #watch(stream: ServerHttp2Stream, bytes: Uint8Array): void {
    const closed = new Promise<void>(resolve => stream.once('close', () => resolve()));
    const watch: FakeWatch = {
      request: fromBinary(WatchRequestSchema, bytes),
      closed,
      send: frame => {
        stream.write(grpcFrame(frame));
      },
      end: (code, detail) => end(stream, code, detail),
    };
    const waiter = this.#watchWaiters.shift();
    if (waiter === undefined) this.#watches.push(watch);
    else waiter(watch);
  }

  #subscribe(stream: ServerHttp2Stream, bytes: Uint8Array): void {
    this.#subscribed += 1;
    const session = stream.session;
    const subscribe: FakeSubscribe = {
      request: validatedFromBinary(SubscribeRequestSchema, bytes),
      closed: new Promise<void>(resolve => stream.once('close', () => resolve())),
      send: frame => {
        stream.write(
          grpcFrame(toBinary(SubscribeResponseSchema, create(SubscribeResponseSchema, {frame}))),
        );
      },
      disconnect: () => session?.destroy(),
      end: (code, detail) => end(stream, code, detail),
    };
    const waiter = this.#subscribeWaiters.shift();
    if (waiter === undefined) this.#subscribes.push(subscribe);
    else waiter(subscribe);
  }

  #read(stream: ServerHttp2Stream, bytes: Uint8Array): void {
    const call: FakeRead = {
      request: fromBinary(ReadRequestSchema, bytes),
      closed: new Promise<void>(resolve => stream.once('close', () => resolve())),
      respond: reply => {
        stream.write(grpcFrame(reply));
        end(stream);
      },
      end: (code, detail) => end(stream, code, detail),
    };
    this.readsReceived.push(call.request);
    const waiter = this.#readWaiters.shift();
    if (waiter === undefined) this.#reads.push(call);
    else waiter(call);
  }

  #hello(stream: ServerHttp2Stream, bytes: Uint8Array): void {
    const request = fromBinary(HelloRequestSchema, bytes);
    if (request.apiSchemaHash !== this.schemaHash) {
      end(
        stream,
        9,
        `API hash mismatch: client ${request.apiSchemaHash}, gateway ${this.schemaHash}`,
      );
      return;
    }
    const fault = this.helloFault;
    if (fault instanceof GrpcError) {
      end(stream, fault.code, fault.message);
      return;
    }
    stream.write(
      grpcFrame(
        fault ??
          toBinary(
            HelloResponseSchema,
            create(HelloResponseSchema, {apiSchemaHash: this.schemaHash, gatewayVersion: 'test'}),
          ),
      ),
    );
    end(stream);
  }

  async dial(signal: AbortSignal, force: boolean): Promise<Duplex> {
    if (signal.aborted) throw new Error('dial cancelled');
    this.forces.push(force);
    this.#dialWaiters.shift()?.();
    const incoming = new PassThrough();
    const outgoing = new PassThrough();
    const client = Duplex.from({readable: incoming, writable: outgoing});
    const server = Duplex.from({readable: outgoing, writable: incoming});
    client.on('error', () => {});
    server.on('error', () => {});
    this.#server.emit('connection', server);
    return client;
  }

  nextTail(): Promise<FakeTail> {
    const tail = this.#tails.shift();
    return tail === undefined
      ? new Promise(resolve => this.#waiters.push(resolve))
      : Promise.resolve(tail);
  }

  /** Answer every call of `method` with gRPC status `code` from now on. */
  refuse(method: string, code: number): void {
    this.#refusals.set(method, code);
  }

  nextHostCall(): Promise<FakeHostCall> {
    const call = this.#hostCalls.shift();
    return call === undefined
      ? new Promise(resolve => this.#hostWaiters.push(resolve))
      : Promise.resolve(call);
  }

  nextMutate(): Promise<FakeMutate> {
    const call = this.#mutations.shift();
    return call === undefined
      ? new Promise(resolve => this.#mutateWaiters.push(resolve))
      : Promise.resolve(call);
  }

  nextWatch(): Promise<FakeWatch> {
    const watch = this.#watches.shift();
    return watch === undefined
      ? new Promise(resolve => this.#watchWaiters.push(resolve))
      : Promise.resolve(watch);
  }

  /** How many Subscribe requests the gateway has received, however many are still open. */
  get subscribed(): number {
    return this.#subscribed;
  }

  nextSubscribe(): Promise<FakeSubscribe> {
    const subscribe = this.#subscribes.shift();
    return subscribe === undefined
      ? new Promise(resolve => this.#subscribeWaiters.push(resolve))
      : Promise.resolve(subscribe);
  }

  /** Settles when the client has dialed once more than the dials already taken: before any call. */
  nextDial(): Promise<void> {
    if (this.forces.length > this.#dialsTaken) {
      this.#dialsTaken += 1;
      return Promise.resolve();
    }
    return new Promise(resolve =>
      this.#dialWaiters.push(() => {
        this.#dialsTaken += 1;
        resolve();
      }),
    );
  }

  nextRead(): Promise<FakeRead> {
    const call = this.#reads.shift();
    return call === undefined
      ? new Promise(resolve => this.#readWaiters.push(resolve))
      : Promise.resolve(call);
  }

  close(): void {
    for (const session of this.#sessions) session.destroy();
    this.#server.close();
  }
}

function end(stream: ServerHttp2Stream, code = 0, detail = ''): void {
  stream.once('wantTrailers', () =>
    stream.sendTrailers({'grpc-status': String(code), 'grpc-message': detail}),
  );
  stream.end();
}
