/** The cases the simulated session honors in src/testing/gateway-session.test.ts, on real HTTP/2. */
import {readFileSync} from 'node:fs';
import {createServer, type ServerHttp2Stream} from 'node:http2';
import {resolve} from 'node:path';
import {Duplex, PassThrough} from 'node:stream';
import {test} from 'node:test';
import {FakeClock} from '@vibesys/transport/testing';
import {expect} from 'expect';
import {GrpcError, grpcFrame} from '../src/index.js';
import {Http2GatewaySession} from '../src/shell/grpc.js';

interface Row {
  name: string;
  messages: string[];
  trailers: {status: string | null; message: string};
  unary: {bytes: string} | {error: {code: number; message: string}};
  stream: {received: string[]; error: {code: number; message: string} | null};
}
// The runner compiles the tests into a directory under the package and runs from the package.
const rows: Row[] = JSON.parse(
  readFileSync(resolve('../../../proto/testdata/gateway_session/contract.json'), 'utf8'),
);
const hex = (bytes: Uint8Array): string => Buffer.from(bytes).toString('hex');
const outcome = (error: unknown): {code: number; message: string} | string =>
  error instanceof GrpcError ? {code: error.code, message: error.message} : String(error);

function peer(row: Row): Http2GatewaySession {
  const server = createServer();
  server.on('stream', (stream: ServerHttp2Stream) => {
    stream.on('error', () => {});
    stream.on('data', () => {});
    stream.on('end', () => {
      stream.respond({':status': 200, 'content-type': 'application/grpc'}, {waitForTrailers: true});
      for (const message of row.messages) stream.write(grpcFrame(Buffer.from(message, 'hex')));
      stream.once('wantTrailers', () =>
        stream.sendTrailers(
          row.trailers.status === null
            ? {}
            : {
                'grpc-status': row.trailers.status,
                'grpc-message': encodeURIComponent(row.trailers.message),
              },
        ),
      );
      stream.end();
    });
  });
  const incoming = new PassThrough();
  const outgoing = new PassThrough();
  const client = Duplex.from({readable: incoming, writable: outgoing});
  const remote = Duplex.from({readable: outgoing, writable: incoming});
  client.on('error', () => {});
  remote.on('error', () => {});
  server.emit('connection', remote);
  return new Http2GatewaySession(client, new FakeClock().schedule);
}

for (const row of rows)
  test(`${row.name}: unary and stream end as the contract says`, async () => {
    const unary = peer(row);
    try {
      const actual = await unary
        .unary('/vibesys.api.v1.HostService/Read', new Uint8Array(), new AbortController().signal)
        .then(
          bytes => ({bytes: hex(bytes)}),
          error => ({error: outcome(error)}),
        );
      expect(actual).toEqual(row.unary);
    } finally {
      unary.close();
    }
    const stream = peer(row);
    try {
      const received: string[] = [];
      const error = await stream
        .stream(
          '/vibesys.api.v1.HostService/Tail',
          new Uint8Array(),
          new AbortController().signal,
          message => received.push(hex(message)),
        )
        .then(
          () => null,
          failure => outcome(failure),
        );
      expect(error).toEqual(row.stream.error);
      expect(received).toEqual(row.stream.received);
    } finally {
      stream.close();
    }
  });

test('a closed session tells its owner once, fails calls in flight, and refuses new ones', async () => {
  const hang = createServer();
  hang.on('stream', (stream: ServerHttp2Stream) => {
    stream.on('error', () => {});
    stream.on('data', () => {});
  });
  const incoming = new PassThrough();
  const outgoing = new PassThrough();
  const client = Duplex.from({readable: incoming, writable: outgoing});
  const remote = Duplex.from({readable: outgoing, writable: incoming});
  client.on('error', () => {});
  remote.on('error', () => {});
  hang.emit('connection', remote);
  let lost = 0;
  let told = (): void => {};
  const toldOnce = new Promise<void>(resolve => {
    told = resolve;
  });
  const session = new Http2GatewaySession(client, new FakeClock().schedule, () => {
    lost += 1;
    told();
  });
  const failure = session.unary('/a.B/Read', new Uint8Array(), new AbortController().signal).then(
    () => 'answered',
    error => String(error),
  );
  await new Promise(resolve => setImmediate(resolve));
  session.close();
  session.close();
  // Destroying ends the stream with or without a clean end, so either failure is the contract.
  expect(['Error: gRPC connection closed', 'GrpcError: missing gRPC status']).toContain(
    await failure,
  );
  expect(lost).toBe(0);
  await toldOnce;
  await new Promise(resolve => setImmediate(resolve));
  expect(lost).toBe(1);
  await expect(
    session.unary('/a.B/Read', new Uint8Array(), new AbortController().signal),
  ).rejects.toThrow('The session has been destroyed');
});
