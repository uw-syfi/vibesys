import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {describe, test} from 'node:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {
  API_SCHEMA_HASH,
  encodeHostMutate,
  encodeHostRead,
  GetBlobsResponseSchema,
  HelloResponseSchema,
  HOST_API_MAX_MESSAGE_BYTES,
  HostCallRequestSchema,
  HostService,
  hostCallBudgets,
  MutateRequestSchema,
  mutateBudgets,
  ReadRequestSchema,
  readBudgets,
  TailRequestSchema,
  TailResponseSchema,
  validatedFromBinary,
  validateHostPayload,
} from '@vibesys/proto';
import {Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {expect} from 'expect';
import {
  GatewayCalls,
  GatewayConnection,
  GrpcDecoder,
  GrpcError,
  grpcFrame,
  HELLO_METHOD,
  MUTATE_METHOD,
  READ_METHOD,
  TAIL_METHOD,
} from '../src/index.js';
// test-isolation: Node HTTP/2 needs the gateway Fake alone; legacy contract exports load bun:test.
import {FakeGateway} from '../src/testing/fake-gateway.js';

const request = () =>
  toBinary(TailRequestSchema, create(TailRequestSchema, {runId: 'run-a', fromSeq: 1n}));
const response = (seq: bigint) =>
  toBinary(
    TailResponseSchema,
    create(TailResponseSchema, {throughSeq: seq, records: [new Uint8Array([Number(seq)])]}),
  );

for (const fault of ['malformed protobuf', 'gateway_version', 'rejected version'] as const) {
  test(`invalid Hello ${fault} ends the call with no reconnect`, {timeout: 15000}, async () => {
    const fake = new FakeGateway();
    fake.helloFault =
      fault === 'malformed protobuf'
        ? new Uint8Array([255])
        : fault === 'rejected version'
          ? new GrpcError(13, 'gateway_version must be nonempty')
          : toBinary(
              HelloResponseSchema,
              create(HelloResponseSchema, {apiSchemaHash: API_SCHEMA_HASH}),
            );
    const clock = new FakeClock();
    const connection = new GatewayConnection(fake, clock.schedule);
    try {
      await expect(
        connection.stream(TAIL_METHOD, request(), new AbortController().signal, () => {}),
      ).rejects.toThrow(
        fault === 'malformed protobuf' ? 'invalid Hello response' : 'gateway_version',
      );
      expect(fake.forces).toEqual([false]);
      expect(clock.pendingDelays()).toEqual([]);
    } finally {
      connection.close();
      fake.close();
    }
  });
}

describe('gateway public transport', () => {
  test('gRPC frames survive generated fragmentation and coalescing', () => {
    for (let seed = 1; seed <= 50; seed += 1) {
      const random = new Seeded(seed);
      const messages = Array.from({length: random.int(20) + 1}, () =>
        Uint8Array.from({length: random.int(300)}, () => random.int(256)),
      );
      const input = Buffer.concat(messages.map(grpcFrame));
      const decoder = new GrpcDecoder();
      const output: Uint8Array[] = [];
      for (let offset = 0; offset < input.length; ) {
        const end = offset + random.int(150) + 1;
        output.push(...decoder.push(input.subarray(offset, end)));
        offset = end;
      }
      decoder.finish();
      expect(output.map(bytes => [...bytes])).toEqual(messages.map(bytes => [...bytes]));
    }
  });

  test('rejects compression, oversized lengths, and truncated frames', () => {
    expect(() => new GrpcDecoder().push(new Uint8Array([1, 0, 0, 0, 0]))).toThrow('compressed');
    expect(() => new GrpcDecoder().push(new Uint8Array([0, 2, 0, 0, 0]))).toThrow(
      String(HOST_API_MAX_MESSAGE_BYTES),
    );
    for (let length = 1; length < 8; length += 1) {
      const decoder = new GrpcDecoder();
      decoder.push(grpcFrame(new Uint8Array(3)).subarray(0, length));
      expect(() => decoder.finish()).toThrow('truncated');
    }
  });

  test('Hello precedes Tail; loss reconnects once and resumes at the last accepted sequence plus one', {
    timeout: 15000,
  }, async () => {
    const fake = new FakeGateway();
    const clock = new FakeClock();
    const connection = new GatewayConnection(fake, clock.scheduleTimeout);
    const abort = new AbortController();
    let delivered = (): void => {};
    let delivery = new Promise<void>(resolve => {
      delivered = resolve;
    });
    const seen: Uint8Array[] = [];
    const result = connection.stream(TAIL_METHOD, request(), abort.signal, bytes => {
      seen.push(bytes);
      delivered();
    });
    const outcome = result.catch(error => String(error));
    try {
      const first = await fake.nextTail();
      expect(first.fromSeq).toBe(1n);
      first.send(response(10n));
      await delivery;
      first.disconnect();
      await clock.waitUntilScheduled(500);
      clock.advanceBy(500);
      const resumed = await fake.nextTail();
      expect(resumed.fromSeq).toBe(11n);
      delivery = new Promise(resolve => {
        delivered = resolve;
      });
      resumed.send(response(11n));
      await delivery;
      expect(seen).toHaveLength(2);
      expect(fake.forces).toEqual([false, false]);
      abort.abort();
      expect(await outcome).toContain('Abort');
    } finally {
      abort.abort();
      connection.close();
      fake.close();
    }
  });

  test('a schema mismatch replaces once and reports both hashes without retrying forever', {
    timeout: 15000,
  }, async () => {
    const fake = new FakeGateway();
    fake.schemaHash = 'stale-api';
    const clock = new FakeClock();
    const connection = new GatewayConnection(fake, clock.scheduleTimeout);
    try {
      await expect(
        connection.stream(TAIL_METHOD, request(), new AbortController().signal, () => {}),
      ).rejects.toThrow('stale-api');
      expect(fake.forces).toEqual([false, true]);
      expect(clock.pendingDelays()).toEqual([]);
    } finally {
      connection.close();
      fake.close();
    }
  });
});

test('generic bridge opt-in gates dialing and two live calls share a single host session', {
  timeout: 15000,
}, async () => {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  let enabled = false;
  const calls = new GatewayCalls(
    async () => enabled,
    () => fake,
    clock.schedule,
  );
  await expect(
    calls.unary('local', HELLO_METHOD, new Uint8Array(), new AbortController().signal),
  ).rejects.toThrow('off');
  expect(fake.forces).toEqual([]);
  enabled = true;
  const first = new AbortController();
  const second = new AbortController();
  const one = calls
    .stream('local', TAIL_METHOD, request(), first.signal, () => {})
    .catch(error => String(error));
  const two = calls
    .stream('local', TAIL_METHOD, request(), second.signal, () => {})
    .catch(error => String(error));
  try {
    await Promise.all([fake.nextTail(), fake.nextTail()]);
    expect(fake.forces).toEqual([false]);
    first.abort();
    await one;
    expect(fake.forces).toEqual([false]);
    calls.disable('local');
    expect(await two).toContain('closed');
    expect(clock.pendingDelays()).toEqual([]);
  } finally {
    first.abort();
    second.abort();
    calls.disable('local');
    fake.close();
  }
});

test('disabling a host fences an opt-in read already in flight', {timeout: 15000}, async () => {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  let release = (_enabled: boolean): void => {};
  const enabled = new Promise<boolean>(resolve => {
    release = resolve;
  });
  const calls = new GatewayCalls(
    () => enabled,
    () => fake,
    clock.schedule,
  );
  const call = calls.unary('local', HELLO_METHOD, new Uint8Array(), new AbortController().signal);
  calls.disable('local');
  release(true);
  await expect(call).rejects.toThrow('cancelled');
  expect(fake.forces).toEqual([]);
  fake.close();
});

test('unary deadlines cancel a peer that leaves its response open without replaying the call', {
  timeout: 15000,
}, async () => {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const connection = new GatewayConnection(fake, clock.schedule);
  const result = connection.unary(TAIL_METHOD, request(), new AbortController().signal);
  const outcome = result.catch(error => String(error));
  try {
    await fake.nextTail();
    await clock.waitUntilScheduled(15_000);
    clock.advanceBy(15_000);
    expect(await outcome).toContain('deadline');
    expect(fake.forces).toEqual([false]);
  } finally {
    connection.close();
    fake.close();
  }
});

test('cancellation settles while a host-setting read is pending and discards its late answer', {
  timeout: 15000,
}, async () => {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  let release = (_enabled: boolean): void => {};
  const enabled = new Promise<boolean>(resolve => {
    release = resolve;
  });
  const calls = new GatewayCalls(
    () => enabled,
    () => fake,
    clock.schedule,
  );
  const abort = new AbortController();
  const call = calls.unary('local', HELLO_METHOD, new Uint8Array(), abort.signal);
  abort.abort();
  await expect(call).rejects.toThrow('cancelled');
  release(true);
  expect(fake.forces).toEqual([]);
  expect(clock.pendingDelays()).toEqual([]);
  fake.close();
});

test('closing the bridge cancels calls waiting on settings and live Tail calls', {
  timeout: 15000,
}, async () => {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const calls = new GatewayCalls(
    async () => true,
    () => fake,
    clock.schedule,
  );
  const result = calls
    .stream('local', TAIL_METHOD, request(), new AbortController().signal, () => {})
    .catch(error => String(error));
  await fake.nextTail();
  calls.close();
  expect(await result).toContain('Abort');
  expect(clock.pendingDelays()).toEqual([]);
  await expect(
    calls.unary('local', HELLO_METHOD, new Uint8Array(), new AbortController().signal),
  ).rejects.toThrow('closed');
  fake.close();
});

for (const fault of ['malformed protobuf', 'schema violation', 'consumer failure'] as const) {
  test(`${fault} fails a Tail call without reconnecting a healthy host`, {
    timeout: 15000,
  }, async () => {
    const fake = new FakeGateway();
    const clock = new FakeClock();
    const connection = new GatewayConnection(fake, clock.schedule);
    const result = connection
      .stream(TAIL_METHOD, request(), new AbortController().signal, () => {
        if (fault === 'consumer failure') throw new Error('consumer stopped');
      })
      .catch(error => String(error));
    try {
      const tail = await fake.nextTail();
      tail.send(
        fault === 'malformed protobuf'
          ? new Uint8Array([255])
          : fault === 'schema violation'
            ? toBinary(TailResponseSchema, create(TailResponseSchema, {throughSeq: 1n}))
            : response(1n),
      );
      expect(await result).toContain(
        fault === 'consumer failure' ? 'consumer failed' : 'invalid Tail response',
      );
      expect(fake.forces).toEqual([false]);
      expect(clock.pendingDelays()).toEqual([]);
    } finally {
      connection.close();
      fake.close();
    }
  });
}

function hostCallRequest(method: string): Uint8Array {
  const project = {projectRoot: '/project', runId: 'run-a'};
  if (method === 'postIntent') {
    return toBinary(
      HostCallRequestSchema,
      create(HostCallRequestSchema, {
        checkout: '/checkout',
        method: {
          case: 'postIntent',
          value: {
            ...project,
            requestId: 'id-a',
            expectedEpoch: 1n,
            intent: {case: 'pause', value: {}},
          },
        },
      }),
    );
  }
  if (method === 'relaunch') {
    return toBinary(
      HostCallRequestSchema,
      create(HostCallRequestSchema, {
        checkout: '/checkout',
        method: {case: 'relaunch', value: {...project, expectedEpoch: 1n}},
      }),
    );
  }
  if (method === 'chatMessage') {
    return toBinary(
      HostCallRequestSchema,
      create(HostCallRequestSchema, {
        checkout: '/checkout',
        method: {
          case: 'chatMessage',
          value: {...project, sessionId: 'default', requestId: 'q-1', text: 'Why?'},
        },
      }),
    );
  }
  throw new Error(`no host-call payload for ${method}`);
}

for (const [method, budget] of Object.entries(hostCallBudgets)) {
  test(`host ${method} waits for the registry budget above the runner cap`, {
    timeout: 15000,
  }, async () => {
    expect(budget.totalMs).toBeGreaterThan(budget.capMs);
    expect(budget.totalMs).toBe(budget.capMs + budget.graceMs);
    const fake = new FakeGateway();
    const clock = new FakeClock();
    const connection = new GatewayConnection(fake, clock.schedule);
    const pending = connection
      .unary(
        `/${HostService.typeName}/${HostService.method.hostCall.name}`,
        hostCallRequest(method),
        new AbortController().signal,
      )
      .catch(error => String(error));
    try {
      await fake.nextHostCall();
      await clock.waitUntilScheduled(budget.totalMs);
      clock.advanceBy(budget.totalMs);
      expect(await pending).toContain('deadline');
    } finally {
      connection.close();
      fake.close();
    }
  });
}

const cluster = {
  name: 'cluster-a',
  workspace: '/work',
  reach: {value: {case: 'sshReach', value: {kind: 'ssh', host: 'host-a'}}},
};
const deadlineInputs = {
  listTasks: {project: '/project'},
  listSavedRuns: {project: '/project'},
  listHarnesses: {},
  listExamples: {},
  getTaskGoal: {project: '/project', task: 'bench'},
  describeMachine: {},
  listCompute: {},
  preflightRun: {project: '/project'},
  inspectCandidate: {path: '/candidate', selectionRevision: 1n},
  completePaths: {prefix: '/manual/'},
  inspectDestination: {example: 'queue', destination: '/work/queue'},
  createExample: {example: 'queue', destination: '/work/queue'},
  copyProject: {source: '/project', destination: '/work/copy'},
  putCluster: {cluster},
  removeCluster: {name: 'cluster-a'},
  testCluster: {cluster},
  startRun: {project: '/project'},
  terminateRun: {
    projectRoot: '/project',
    runId: 'run-a',
    expectedProcess: {pid: 42, epoch: 1, startedAt: {seconds: 1n, nanos: 0}},
  },
} satisfies Record<keyof typeof readBudgets | keyof typeof mutateBudgets, object>;

for (const {kind, schema, budgets, method, encode, nextCall} of [
  {
    kind: 'read',
    schema: ReadRequestSchema,
    budgets: readBudgets,
    method: READ_METHOD,
    encode: encodeHostRead,
    nextCall: (fake: FakeGateway) => fake.nextRead(),
  },
  {
    kind: 'mutate',
    schema: MutateRequestSchema,
    budgets: mutateBudgets,
    method: MUTATE_METHOD,
    encode: encodeHostMutate,
    nextCall: (fake: FakeGateway) => fake.nextMutate(),
  },
]) {
  for (const [slot, budget] of Object.entries(budgets)) {
    test(`${kind} ${slot} waits for the registry budget above the runner cap`, {
      timeout: 15000,
    }, async () => {
      expect(budget.totalMs).toBeGreaterThan(budget.capMs);
      expect(budget.totalMs).toBe(budget.capMs + budget.graceMs);
      const field = schema.fields.find(field => field.localName === slot);
      if (field?.fieldKind !== 'message') throw new Error(`no ${kind} payload for ${slot}`);
      const name = `${slot[0]?.toUpperCase() ?? ''}${slot.slice(1)}`;
      const fake = new FakeGateway();
      const clock = new FakeClock();
      const connection = new GatewayConnection(fake, clock.schedule);
      const input = deadlineInputs[slot as keyof typeof deadlineInputs];
      const payload = toBinary(
        field.message,
        create(field.message, {requestId: 'request-a', ...input}),
      );
      const bytes = encode('/checkout', name, payload);
      validateHostPayload(method, 'input', bytes);
      const pending = connection
        .unary(method, bytes, new AbortController().signal)
        .catch(error => String(error));
      try {
        await nextCall(fake);
        await clock.waitUntilScheduled(budget.totalMs);
        clock.advanceBy(budget.totalMs);
        expect(await pending).toContain('deadline');
      } finally {
        connection.close();
        fake.close();
      }
    });
  }
}

test('a maximum-size blob plus response metadata fits the shared gateway frame budget', () => {
  const data = new Uint8Array(16 * 1024 * 1024);
  const response = create(GetBlobsResponseSchema, {blobs: [{sha256: 'a'.repeat(64), data}]});
  const bytes = toBinary(GetBlobsResponseSchema, response);
  const decoder = new GrpcDecoder();
  const decoded = decoder.push(grpcFrame(bytes));
  decoder.finish();
  expect(decoded).toHaveLength(1);
  expect(
    validatedFromBinary(GetBlobsResponseSchema, decoded[0] as Uint8Array).blobs[0]?.data.byteLength,
  ).toBe(data.byteLength);
});

// Both Go's in-process gateway mapping and the deterministic seam host use this same corpus.
test('every shared status and percent-encoded diagnostic crosses real HTTP/2', {
  timeout: 15000,
}, async () => {
  const rows: Array<{code: number; message: string}> = JSON.parse(
    // The runner compiles the tests into a directory under the package and runs from the package.
    readFileSync(resolve('../../../proto/testdata/grpc_status/statuses.json'), 'utf8'),
  );
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const connection = new GatewayConnection(fake, clock.schedule);
  try {
    for (const row of rows) {
      const outcome = connection
        .stream(TAIL_METHOD, request(), new AbortController().signal, () => {})
        .then(
          () => undefined,
          error => ({code: error.code, message: error.message}),
        );
      const call = await fake.nextTail();
      call.end(row.code, row.message);
      expect(await outcome).toEqual(
        row.code === 0 ? undefined : {code: row.code, message: row.message},
      );
    }
    expect(fake.forces).toEqual([false]);
  } finally {
    connection.close();
    fake.close();
  }
});
