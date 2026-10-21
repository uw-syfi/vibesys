/** A real in-process HTTP/2 peer over memory duplexes, with controllable Tail delivery. */
import {createServer, type ServerHttp2Session, type ServerHttp2Stream} from 'node:http2';
import {Duplex, PassThrough} from 'node:stream';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  API_SCHEMA_HASH,
  HelloRequestSchema,
  HelloResponseSchema,
  type HostCallRequest,
  HostCallRequestSchema,
  HostService,
  TailRequestSchema,
} from '@vibesys/proto';
import {type GatewayDialer, HELLO_METHOD, TAIL_METHOD} from '../shell/gateway.js';
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

export class FakeGateway implements GatewayDialer {
  readonly forces: boolean[] = [];
  schemaHash = API_SCHEMA_HASH;
  readonly #server = createServer();
  readonly #sessions = new Set<ServerHttp2Session>();
  readonly #tails: FakeTail[] = [];
  readonly #hostCalls: FakeHostCall[] = [];
  readonly #hostWaiters: Array<(call: FakeHostCall) => void> = [];
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
        decoder.finish();
        const method = headers[':path'];
        stream.respond(
          {':status': 200, 'content-type': 'application/grpc'},
          {waitForTrailers: true},
        );
        if (method === HELLO_METHOD) {
          this.#hello(stream, messages[0] as Uint8Array);
        } else if (method === TAIL_METHOD) {
          const request = fromBinary(TailRequestSchema, messages[0] as Uint8Array);
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
        } else if (method === `/${HostService.typeName}/${HostService.method.hostCall.name}`) {
          this.#hostCall(stream, messages[0] as Uint8Array);
        } else end(stream, 12, 'unknown method');
      });
    });
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
    stream.write(
      grpcFrame(
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

  nextHostCall(): Promise<FakeHostCall> {
    const call = this.#hostCalls.shift();
    return call === undefined
      ? new Promise(resolve => this.#hostWaiters.push(resolve))
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
