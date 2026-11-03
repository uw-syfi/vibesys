/**
 * A real in-process HTTP/2 gRPC peer over memory duplexes, standing in for a `vibesysd`. It speaks
 * the same framing the client does and answers the handshake; a test scripts the other methods.
 */
import {createServer, type ServerHttp2Session, type ServerHttp2Stream} from 'node:http2';
import {Duplex, PassThrough} from 'node:stream';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  API_SCHEMA_HASH,
  HelloRequestSchema,
  HelloResponseSchema,
  HOST_API_MAX_MESSAGE_BYTES,
  HostService,
} from '@vibesys/proto';
import {decodeFrames, encodeFrame, isFault} from '../../state/grpc-frames.js';

export const HELLO = `/${HostService.typeName}/${HostService.method.hello.name}`;

/** One call the peer received on a scripted method. */
interface FakeCall {
  readonly method: string;
  readonly request: Uint8Array;
  /** Send one reply message. */
  send(message: Uint8Array): void;
  /** End the call with `code` (OK by default). */
  end(code?: number, detail?: string): void;
  /** Whether the client reset or closed this call. */
  readonly cancelled: () => boolean;
}

export interface FakeGateway {
  /** The handshake hash the peer answers with; defaults to the client's. */
  schemaHash: string;
  /** Calls received on methods other than the handshake, oldest first. */
  readonly calls: FakeCall[];
  /** Everything the peer received, handshake included, oldest first. */
  readonly methods: string[];
  /** A new connection to the peer: the client end of a memory duplex pair. */
  connect(): Duplex;
  /** End every open connection, as a dropped link does. */
  drop(): void;
}

/** A pair of duplexes wired back to back. */
function duplexPair(): [Duplex, Duplex] {
  const aToB = new PassThrough();
  const bToA = new PassThrough();
  return [
    Duplex.from({readable: bToA, writable: aToB}),
    Duplex.from({readable: aToB, writable: bToA}),
  ];
}

export function fakeGateway(): FakeGateway {
  const server = createServer();
  const sessions = new Set<ServerHttp2Session>();
  const sockets = new Set<Duplex>();
  const calls: FakeCall[] = [];
  const methods: string[] = [];
  const gateway: FakeGateway = {
    schemaHash: API_SCHEMA_HASH,
    calls,
    methods,
    connect: () => {
      const [client, peer] = duplexPair();
      // Destroying one end aborts the pair; no one is left to read that as a fault.
      client.on('error', () => {});
      peer.on('error', () => {});
      sockets.add(client);
      sockets.add(peer);
      server.emit('connection', peer);
      return client;
    },
    drop: () => {
      for (const session of sessions) session.destroy();
      for (const socket of sockets) socket.destroy();
      sockets.clear();
    },
  };
  server.on('session', session => {
    sessions.add(session);
    session.on('error', () => {});
    session.once('close', () => sessions.delete(session));
  });
  server.on('stream', (stream: ServerHttp2Stream, headers) => {
    stream.on('error', () => {});
    let rest: Uint8Array = new Uint8Array(0);
    const messages: Uint8Array[] = [];
    stream.on('data', (chunk: Buffer) => {
      const decoded = decodeFrames(rest, chunk, HOST_API_MAX_MESSAGE_BYTES);
      if (isFault(decoded)) return;
      rest = decoded.rest;
      messages.push(...decoded.messages);
    });
    stream.on('end', () => {
      if (stream.destroyed) return;
      const method = String(headers[':path']);
      methods.push(method);
      stream.respond({':status': 200, 'content-type': 'application/grpc'}, {waitForTrailers: true});
      const finish = (code: number, detail: string): void => {
        if (stream.destroyed || stream.closed) return;
        stream.once('wantTrailers', () =>
          stream.sendTrailers({'grpc-status': String(code), 'grpc-message': detail}),
        );
        stream.end();
      };
      const request = messages[0] ?? new Uint8Array(0);
      if (method === HELLO) {
        fromBinary(HelloRequestSchema, request);
        const reply = encodeFrame(
          toBinary(
            HelloResponseSchema,
            create(HelloResponseSchema, {
              apiSchemaHash: gateway.schemaHash,
              gatewayVersion: 'test',
            }),
          ),
          HOST_API_MAX_MESSAGE_BYTES,
        );
        if (!isFault(reply)) stream.write(reply);
        finish(0, '');
        return;
      }
      calls.push({
        method,
        request,
        send: message => {
          const frame = encodeFrame(message, HOST_API_MAX_MESSAGE_BYTES);
          if (!isFault(frame) && !stream.destroyed) stream.write(frame);
        },
        end: (code = 0, detail = '') => finish(code, detail),
        cancelled: () => stream.destroyed,
      });
    });
  });
  return gateway;
}
