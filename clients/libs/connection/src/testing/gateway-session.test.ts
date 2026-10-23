/** The simulated session honors the cases the real HTTP/2 session passes in node-tests. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {PassThrough} from 'node:stream';
import {FakeClock} from '@vibesys/transport/testing';
import {GrpcError} from '../shell/grpc.js';
import {FakeGatewaySession, type GatewayWireCall} from './gateway-session.js';
import {Schedule} from './schedule.js';

interface Row {
  name: string;
  messages: string[];
  trailers: {status: string | null; message: string};
  unary: {bytes: string} | {error: {code: number; message: string}};
  stream: {received: string[]; error: {code: number; message: string} | null};
}
const rows: Row[] = JSON.parse(
  readFileSync(
    new URL('../../../../../proto/testdata/gateway_session/contract.json', import.meta.url),
    'utf8',
  ),
);
const hex = (bytes: Uint8Array): string => Buffer.from(bytes).toString('hex');
const outcome = (error: unknown): {code: number; message: string} | string =>
  error instanceof GrpcError ? {code: error.code, message: error.message} : String(error);

function session(seed: number, row: Row): {schedule: Schedule; session: FakeGatewaySession} {
  const clock = new FakeClock();
  const schedule = new Schedule(seed, clock);
  const peer = (call: GatewayWireCall): void => {
    for (const message of row.messages) call.message(Buffer.from(message, 'hex'));
    call.trailers(row.trailers.status ?? undefined, encodeURIComponent(row.trailers.message));
  };
  return {
    schedule,
    session: new FakeGatewaySession(new PassThrough(), schedule, peer, clock.schedule),
  };
}

for (const row of rows)
  test(`${row.name}: unary and stream end as the real session ends them`, async () => {
    for (const seed of [1, 7, 23, 42]) {
      const unary = session(seed, row);
      const settled = unary.session
        .unary('/vibesys.api.v1.HostService/Read', new Uint8Array(), new AbortController().signal)
        .then(
          bytes => ({bytes: hex(bytes)}),
          error => ({error: outcome(error)}),
        );
      expect(await unary.schedule.run(settled), `unary seed ${seed}`).toEqual(row.unary);

      const stream = session(seed, row);
      const received: string[] = [];
      const done = stream.session
        .stream(
          '/vibesys.api.v1.HostService/Tail',
          new Uint8Array(),
          new AbortController().signal,
          message => received.push(hex(message)),
        )
        .then(
          () => null,
          error => outcome(error),
        );
      expect(await stream.schedule.run(done), `stream seed ${seed}`).toEqual(row.stream.error);
      expect(received, `stream seed ${seed}`).toEqual(row.stream.received);
    }
  });

test('a method outside the gateway grammar and an aborted signal fail before any byte is sent', async () => {
  const row = rows[0] as Row;
  const {session: fake} = session(1, row);
  await expect(
    fake.stream('Read', new Uint8Array(), new AbortController().signal, () => {}),
  ).rejects.toThrow('invalid gRPC method');
  const aborted = new AbortController();
  aborted.abort(new Error('caller left'));
  await expect(fake.unary('/a.B/Read', new Uint8Array(), aborted.signal)).rejects.toThrow(
    'caller left',
  );
});

test('a closed session tells its owner once, fails calls in flight, and refuses new ones', async () => {
  const clock = new FakeClock();
  const schedule = new Schedule(5, clock);
  let lost = 0;
  const silent = new FakeGatewaySession(
    new PassThrough(),
    schedule,
    () => {},
    clock.schedule,
    () => {
      lost += 1;
    },
  );
  const inFlight = silent.unary('/a.B/Read', new Uint8Array(), new AbortController().signal);
  const failure = inFlight.then(
    () => 'answered',
    error => String(error),
  );
  silent.drop();
  silent.close();
  expect(await schedule.run(failure)).toBe('Error: gRPC connection closed');
  expect(lost).toBe(0);
  await schedule.quiesce();
  expect(lost).toBe(1);
  await expect(
    silent.unary('/a.B/Read', new Uint8Array(), new AbortController().signal),
  ).rejects.toThrow('The session has been destroyed');
});
