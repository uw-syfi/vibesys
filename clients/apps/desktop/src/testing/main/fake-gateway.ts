/**
 * A real in-process HTTP/2 gRPC peer over memory duplexes, standing in for a `vibesys-host`. It speaks
 * the same framing the client does and answers the handshake; a test scripts the other methods.
 */
import {
  createServer,
  type IncomingHttpHeaders,
  type ServerHttp2Session,
  type ServerHttp2Stream,
} from 'node:http2';
import {Duplex, Transform} from 'node:stream';
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
  /** End the call's stream with trailers that carry no gRPC status, as a cut link can. */
  endWithoutStatus(): void;
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
  /**
   * Stop delivering bytes in both directions on every connection open now, as a link that hangs
   * does: nothing is lost and nothing arrives. A connection made afterwards is healthy.
   */
  hang(): void;
  /** Deliver what a hang held back, and carry on. */
  resume(): void;
  /** Called with each call received on a scripted method, as it arrives. */
  onCall: ((call: FakeCall) => void) | undefined;
}

/** A pipe that holds every chunk back while its connection is hung. */
function gate(hold: {held: boolean}, waiting: Set<() => void>): Transform {
  return new Transform({
    transform(chunk, _encoding, callback) {
      if (!hold.held) return callback(null, chunk);
      const release = (): void => callback(null, chunk);
      waiting.add(release);
    },
  });
}

/** A pair of duplexes wired back to back through pipes that a hang can hold. */
function duplexPair(hold: {held: boolean}, waiting: Set<() => void>): [Duplex, Duplex] {
  const aToB = gate(hold, waiting);
  const bToA = gate(hold, waiting);
  return [
    Duplex.from({readable: bToA, writable: aToB}),
    Duplex.from({readable: aToB, writable: bToA}),
  ];
}

/** What the peer does with each request it receives. */
interface Serving {
  readonly schemaHash: () => string;
  /** The peer saw a request for `method` (handshake included). */
  readonly seen: (method: string) => void;
  /** A call on a scripted method arrived. */
  readonly arrived: (call: FakeCall) => void;
}

/** Read one request off `stream`, answer the handshake, and hand any other call to `serving`. */
function serve(stream: ServerHttp2Stream, headers: IncomingHttpHeaders, serving: Serving): void {
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
    serving.seen(method);
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
            apiSchemaHash: serving.schemaHash(),
            gatewayVersion: 'test',
          }),
        ),
        HOST_API_MAX_MESSAGE_BYTES,
      );
      if (!isFault(reply)) stream.write(reply);
      finish(0, '');
      return;
    }
    serving.arrived({
      method,
      request,
      send: message => {
        const frame = encodeFrame(message, HOST_API_MAX_MESSAGE_BYTES);
        if (!isFault(frame) && !stream.destroyed) stream.write(frame);
      },
      end: (code = 0, detail = '') => finish(code, detail),
      endWithoutStatus: () => {
        if (stream.destroyed || stream.closed) return;
        stream.once('wantTrailers', () => stream.sendTrailers({}));
        stream.end();
      },
      cancelled: () => stream.destroyed,
    });
  });
}

export function fakeGateway(): FakeGateway {
  const server = createServer();
  const sessions = new Set<ServerHttp2Session>();
  const sockets = new Set<Duplex>();
  const calls: FakeCall[] = [];
  const methods: string[] = [];
  const waiting = new Set<() => void>();
  const holds = new Set<{held: boolean}>();
  const gateway: FakeGateway = {
    onCall: undefined,
    hang: () => {
      for (const hold of holds) hold.held = true;
    },
    resume: () => {
      for (const hold of holds) hold.held = false;
      const release = [...waiting];
      waiting.clear();
      for (const next of release) next();
    },
    schemaHash: API_SCHEMA_HASH,
    calls,
    methods,
    connect: () => {
      const hold = {held: false};
      holds.add(hold);
      const [client, peer] = duplexPair(hold, waiting);
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
  server.on('stream', (stream: ServerHttp2Stream, headers) =>
    serve(stream, headers, {
      schemaHash: () => gateway.schemaHash,
      seen: method => methods.push(method),
      arrived: call => {
        calls.push(call);
        gateway.onCall?.(call);
      },
    }),
  );
  return gateway;
}
