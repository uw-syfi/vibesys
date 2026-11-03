import {test} from 'node:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  CommandSchema,
  DispatchRequestSchema,
  DispatchResponseSchema,
  OrchestratorService,
  ReceiptSchema,
  RunsSnapshotSchema,
  WatchAgentRequestSchema,
  WatchRunsRequestSchema,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import {FakeClock} from '@vibesys/transport/testing';
import {expect} from 'expect';
import {GatewayConnection} from '../src/index.js';
// test-isolation: Node HTTP/2 needs the gateway Fake alone; legacy contract exports load bun:test.
import {FakeGateway} from '../src/testing/fake-gateway.js';

const method = (name: string): string => `/${OrchestratorService.typeName}/${name}`;
const WATCH_RUNS = method('WatchRuns');
const WATCH_AGENT = method('WatchAgent');
const DISPATCH = method('Dispatch');

const watchRuns = (afterSequence: bigint): Uint8Array =>
  toBinary(WatchRunsRequestSchema, create(WatchRunsRequestSchema, {afterSequence}));
const snapshotFrame = (sequence: bigint): Uint8Array =>
  toBinary(
    WatchRunsResponseSchema,
    create(WatchRunsResponseSchema, {
      body: {case: 'snapshot', value: create(RunsSnapshotSchema, {sequence})},
    }),
  );

async function withConnection(
  body: (fake: FakeGateway, connection: GatewayConnection, clock: FakeClock) => Promise<void>,
): Promise<void> {
  const fake = new FakeGateway();
  fake.serve(WATCH_RUNS);
  fake.serve(WATCH_AGENT);
  fake.serve(DISPATCH);
  const clock = new FakeClock();
  const connection = new GatewayConnection(fake, clock.scheduleTimeout);
  try {
    await body(fake, connection, clock);
  } finally {
    connection.close();
    fake.close();
  }
}

test('a unary orchestrator call rides the session HostService opened', {
  timeout: 15000,
}, async () => {
  await withConnection(async (fake, connection) => {
    const command = create(CommandSchema, {
      id: 'cmd-1',
      body: {case: 'sendMessage', value: {agentId: 'a', text: 'hi'}},
    });
    const request = toBinary(DispatchRequestSchema, create(DispatchRequestSchema, {command}));
    const reply = connection.unary(DISPATCH, request, new AbortController().signal);
    const served = await fake.nextServed(DISPATCH);
    expect(fromBinary(DispatchRequestSchema, served.request).command?.id).toBe('cmd-1');
    served.send(
      toBinary(
        DispatchResponseSchema,
        create(DispatchResponseSchema, {
          receipt: create(ReceiptSchema, {commandId: 'cmd-1', commandDigest: 'b'.repeat(64)}),
        }),
      ),
    );
    served.end();
    expect(fromBinary(DispatchResponseSchema, await reply).receipt?.commandId).toBe('cmd-1');
    // One Hello, the HostService handshake, and nothing else dialed.
    expect(fake.forces).toEqual([false]);
  });
});

test('a stream delivers frames that hold their schema and fails on one that does not', {
  timeout: 15000,
}, async () => {
  await withConnection(async (fake, connection) => {
    const seen: bigint[] = [];
    const ended = connection
      .stream(WATCH_RUNS, watchRuns(0n), new AbortController().signal, bytes => {
        const body = fromBinary(WatchRunsResponseSchema, bytes).body;
        if (body.case === 'snapshot' || body.case === 'event') seen.push(body.value.sequence);
      })
      .catch(error => String(error));
    const served = await fake.nextServed(WATCH_RUNS);
    served.send(snapshotFrame(4n));
    // An empty response has no body, which the schema requires.
    served.send(toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema)));
    expect(await ended).toContain('invalid WatchRuns response');
    expect(seen).toEqual([4n]);
  });
});

test('a request that breaks its schema is refused before it is sent', {
  timeout: 15000,
}, async () => {
  await withConnection(async (_fake, connection) => {
    const emptyAgent = toBinary(
      WatchAgentRequestSchema,
      create(WatchAgentRequestSchema, {pageSize: 10}),
    );
    await expect(
      connection.stream(WATCH_AGENT, emptyAgent, new AbortController().signal, () => {}),
    ).rejects.toThrow('agent_id');
    await expect(
      connection.unary(method('Elsewhere'), new Uint8Array(), new AbortController().signal),
    ).rejects.toThrow('unknown HostService/OrchestratorService method');
  });
});

test('a lost session ends an orchestrator stream, and the next one starts where its caller says', {
  timeout: 15000,
}, async () => {
  await withConnection(async (fake, connection, clock) => {
    const first = connection
      .stream(WATCH_RUNS, watchRuns(0n), new AbortController().signal, () => {})
      .catch(error => error as {code?: number});
    const served = await fake.nextServed(WATCH_RUNS);
    served.send(snapshotFrame(1n));
    served.disconnect();
    expect(((await first) as {code?: number}).code).toBe(14);
    // The session redials; the lost call is not issued again from its first sequence.
    await clock.waitUntilScheduled(500);
    clock.advanceBy(500);
    const second = connection
      .stream(WATCH_RUNS, watchRuns(7n), new AbortController().signal, () => {})
      .catch(() => undefined);
    const reissued = await fake.nextServed(WATCH_RUNS);
    expect(fromBinary(WatchRunsRequestSchema, reissued.request).afterSequence).toBe(7n);
    reissued.end();
    await second;
  });
});

test('a lost session ends an in-flight orchestrator command, which is not sent again on reconnect', {
  timeout: 15000,
}, async () => {
  await withConnection(async (fake, connection, clock) => {
    const dispatch = (id: string): Uint8Array =>
      toBinary(
        DispatchRequestSchema,
        create(DispatchRequestSchema, {
          command: create(CommandSchema, {
            id,
            body: {case: 'sendMessage', value: {agentId: 'a', text: 'hi'}},
          }),
        }),
      );
    const first = connection
      .unary(DISPATCH, dispatch('cmd-1'), new AbortController().signal)
      .catch(error => error as {code?: number});
    const served = await fake.nextServed(DISPATCH);
    expect(fromBinary(DispatchRequestSchema, served.request).command?.id).toBe('cmd-1');
    served.disconnect();
    expect(((await first) as {code?: number}).code).toBe(14);
    // The session redials. The next command the gateway serves is the caller's new one: the lost
    // command was not queued behind it.
    await clock.waitUntilScheduled(500);
    clock.advanceBy(500);
    const second = connection
      .unary(DISPATCH, dispatch('cmd-2'), new AbortController().signal)
      .catch(() => undefined);
    const next = await fake.nextServed(DISPATCH);
    expect(fromBinary(DispatchRequestSchema, next.request).command?.id).toBe('cmd-2');
    next.end();
    await second;
  });
});
