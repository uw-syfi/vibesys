import {describe, expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {
  CommandSchema,
  Compatibility,
  DispatchRequestSchema,
  DispatchResponseSchema,
  decodeValidated,
  encodeMessage,
  HelloRequestSchema,
  HelloResponseSchema,
  OrchestratorService,
  ReceiptSchema,
  RunsSnapshotSchema,
  WatchAgentResponseSchema,
  WatchRunsRequestSchema,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  type GatewayCaller,
  GatewaySource,
  MAX_QUEUED_FRAMES,
  PROTOCOL_VERSION,
  sourceErrorOf,
} from './gateway-source.js';
import {SourceError} from './source.js';

const path = (name: string): string => `/${OrchestratorService.typeName}/${name}`;

/** A stream the test drives: frames it sends and the end it picks. */
interface OpenStream {
  readonly method: string;
  readonly request: Uint8Array;
  readonly receive: (bytes: Uint8Array) => void;
  readonly ended: (error?: string, status?: number) => void;
  cancelled: number;
}

/** A gateway whose answers the test scripts; it records what the source asked. */
class ScriptedGateway implements GatewayCaller {
  readonly streams: OpenStream[] = [];
  readonly unaries: {method: string; request: Uint8Array; cancelled: number}[] = [];
  /** Answers of unary calls by method, in order; a function answer rejects. */
  readonly answers = new Map<string, (Uint8Array | (() => never))[]>();

  answer(method: string, ...replies: (Uint8Array | (() => never))[]): void {
    this.answers.set(path(method), replies);
  }

  unary(_host: string, method: string, request: Uint8Array) {
    const record = {method, request, cancelled: 0};
    this.unaries.push(record);
    const reply = this.answers.get(method)?.shift();
    const result =
      reply === undefined
        ? Promise.reject(new Error(`no scripted answer for ${method}`))
        : typeof reply === 'function'
          ? new Promise<Uint8Array>((_resolve, reject) => {
              try {
                reply();
              } catch (error) {
                reject(error);
              }
            })
          : Promise.resolve(reply);
    return {
      result,
      cancel: () => {
        record.cancelled += 1;
      },
    };
  }

  stream(
    _host: string,
    method: string,
    request: Uint8Array,
    receive: (bytes: Uint8Array) => void,
    ended: (error?: string, status?: number) => void,
  ) {
    const open: OpenStream = {method, request, receive, ended, cancelled: 0};
    this.streams.push(open);
    return {
      cancel: () => {
        open.cancelled += 1;
        if (open.cancelled === 1) ended('cancelled');
      },
    };
  }
}

const gatewayError =
  (message: string, status?: number): (() => never) =>
  () => {
    throw Object.assign(new Error(message), status === undefined ? {} : {status});
  };

const compatible = (compatibility = Compatibility.COMPATIBLE): Uint8Array =>
  encodeMessage(
    HelloResponseSchema,
    create(HelloResponseSchema, {
      daemon: {binaryVersion: 'v9', protocolVersion: 1, minProtocolVersion: 1},
      compatibility,
    }),
  );

const snapshot = (sequence: bigint): Uint8Array =>
  encodeMessage(
    WatchRunsResponseSchema,
    create(WatchRunsResponseSchema, {
      body: {case: 'snapshot', value: create(RunsSnapshotSchema, {sequence})},
    }),
  );

const never = new AbortController().signal;
const HOST = 'ssh:host-a';

/** Read until the stream ends, returning what it yielded and how it ended. */
async function drain<T>(frames: AsyncIterable<T>): Promise<{seen: T[]; error: unknown}> {
  const seen: T[] = [];
  try {
    for await (const frame of frames) seen.push(frame);
  } catch (error) {
    return {seen, error};
  }
  return {seen, error: null};
}

/** Wait until the source has opened `count` streams (it hellos first, so this takes a turn). */
async function opened(gateway: ScriptedGateway, count: number): Promise<void> {
  while (gateway.streams.length < count) await Promise.resolve();
}

describe('the real source reading a stream', () => {
  test('it says hello with this client versions, then asks for the sequence it holds', async () => {
    const gateway = new ScriptedGateway();
    gateway.answer('Hello', compatible());
    const reading = drain(new GatewaySource(gateway, HOST).watchRuns(41n, never));
    await opened(gateway, 1);
    const hello = decodeValidated(
      HelloRequestSchema,
      gateway.unaries[0]?.request ?? new Uint8Array(),
    );
    expect(hello.client?.protocolVersion).toBe(PROTOCOL_VERSION);
    expect(gateway.streams[0]?.method).toBe(path('WatchRuns'));
    const asked = decodeValidated(
      WatchRunsRequestSchema,
      gateway.streams[0]?.request ?? new Uint8Array(),
    );
    expect(asked.afterSequence).toBe(41n);
    gateway.streams[0]?.ended();
    expect((await reading).error).toBeNull();
  });

  test('frames arrive in order and the daemon closing the stream ends it quietly', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.array(fc.bigInt({min: 0n, max: 1000n}), {maxLength: 40}),
        async sequences => {
          const gateway = new ScriptedGateway();
          gateway.answer('Hello', compatible());
          const reading = drain(new GatewaySource(gateway, HOST).watchRuns(0n, never));
          await opened(gateway, 1);
          const stream = gateway.streams[0];
          for (const sequence of sequences) stream?.receive(snapshot(sequence));
          stream?.ended();
          const {seen, error} = await reading;
          expect(error).toBeNull();
          expect(seen.map(body => (body.case === 'snapshot' ? body.value.sequence : -1n))).toEqual(
            sequences,
          );
        },
      ),
    );
  });

  test('frames sent before a failure are delivered first, then the failure names its cause', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.nat({max: 20}),
        fc.integer({min: 0, max: 17}),
        fc.string({minLength: 1, maxLength: 30}),
        async (frames, status, reason) => {
          const gateway = new ScriptedGateway();
          gateway.answer('Hello', compatible());
          const reading = drain(new GatewaySource(gateway, HOST).watchRuns(0n, never));
          await opened(gateway, 1);
          for (let sequence = 0; sequence < frames; sequence++)
            gateway.streams[0]?.receive(snapshot(BigInt(sequence)));
          gateway.streams[0]?.ended(reason, status);
          const {seen, error} = await reading;
          expect(seen).toHaveLength(frames);
          expect(error).toBeInstanceOf(SourceError);
          expect((error as SourceError).message).toContain(reason);
          expect((error as SourceError).kind).toBe(sourceErrorOf(reason, status).kind);
        },
      ),
    );
  });

  test('aborting the signal ends the frames quietly and cancels the call once', async () => {
    const gateway = new ScriptedGateway();
    gateway.answer('Hello', compatible());
    const abort = new AbortController();
    const reading = drain(new GatewaySource(gateway, HOST).watchRuns(0n, abort.signal));
    await opened(gateway, 1);
    gateway.streams[0]?.receive(snapshot(1n));
    abort.abort();
    const {seen, error} = await reading;
    expect(error).toBeNull();
    expect(seen).toHaveLength(1);
    expect(gateway.streams[0]?.cancelled).toBeGreaterThanOrEqual(1);
  });

  test('a reader that stops early releases the call', async () => {
    const gateway = new ScriptedGateway();
    const source = new GatewaySource(gateway, HOST);
    const frames = source.watchAgent('agent-1', 10, 0n, never);
    const first = frames.next();
    gateway.streams[0]?.receive(
      encodeMessage(
        WatchAgentResponseSchema,
        create(WatchAgentResponseSchema, {
          body: {case: 'page', value: {sequence: 3n}},
          sequence: 3n,
        }),
      ),
    );
    expect((await first).done).toBe(false);
    await frames.return(undefined);
    expect(gateway.streams[0]?.cancelled).toBe(1);
  });

  test('a frame that breaks its schema ends the stream as invalid and cancels the call', async () => {
    const gateway = new ScriptedGateway();
    gateway.answer('Hello', compatible());
    const reading = drain(new GatewaySource(gateway, HOST).watchRuns(0n, never));
    await opened(gateway, 1);
    gateway.streams[0]?.receive(snapshot(1n));
    // A response with no body: the schema requires one.
    gateway.streams[0]?.receive(
      encodeMessage(WatchRunsResponseSchema, create(WatchRunsResponseSchema)),
    );
    gateway.streams[0]?.receive(snapshot(2n));
    const {seen, error} = await reading;
    expect(seen).toHaveLength(1);
    expect((error as SourceError).kind).toBe('invalid');
    expect(gateway.streams[0]?.cancelled).toBe(1);
  });

  test('a reader that falls too far behind is cut off, not buffered without bound', async () => {
    const gateway = new ScriptedGateway();
    gateway.answer('Hello', compatible());
    const reading = drain(new GatewaySource(gateway, HOST).watchRuns(0n, never));
    await opened(gateway, 1);
    for (let n = 0; n <= MAX_QUEUED_FRAMES; n++) gateway.streams[0]?.receive(snapshot(1n));
    const {error} = await reading;
    expect((error as SourceError).message).toContain('too far behind');
  });
});

describe('the real source saying why a host cannot be used', () => {
  test.each([
    ['another vibesysd holds the orchestrator (pid 7)', 14, 'lost'],
    ['unknown service vibesys.orchestrator.v1.OrchestratorService', 12, 'refused'],
  ] as const)('a hello answered %s (status %d) is a %s state naming the reason', async (reason, status, kind) => {
    const gateway = new ScriptedGateway();
    gateway.answer('Hello', gatewayError(reason, status));
    const {seen, error} = await drain(new GatewaySource(gateway, HOST).watchRuns(0n, never));
    expect(seen).toEqual([]);
    expect((error as SourceError).kind).toBe(kind);
    expect((error as SourceError).message).toContain(reason);
    expect(gateway.streams).toEqual([]);
  });

  test.each([
    [Compatibility.CLIENT_TOO_OLD, 'update the app'],
    [Compatibility.DAEMON_TOO_OLD, 'update vibesysd'],
  ] as const)('an incompatible hello (%d) says which side to update', async (compatibility, fix) => {
    const gateway = new ScriptedGateway();
    gateway.answer('Hello', compatible(compatibility));
    const {error} = await drain(new GatewaySource(gateway, HOST).watchRuns(0n, never));
    expect((error as SourceError).kind).toBe('refused');
    expect((error as SourceError).message).toContain(fix);
    expect(gateway.streams).toEqual([]);
  });

  test('a call that ends with no status is a lost connection with the transport text', () => {
    const error = sourceErrorOf('gateway closed', undefined);
    expect([error.kind, error.message]).toEqual(['lost', 'gateway closed']);
  });
});

describe('the real source sending a command', () => {
  const command = create(CommandSchema, {
    id: 'cmd-9',
    body: {case: 'sendMessage', value: {agentId: 'a', text: 'hello'}},
  });

  test('the command travels with its id and the receipt comes back whole', async () => {
    const gateway = new ScriptedGateway();
    const receipt = create(ReceiptSchema, {commandId: 'cmd-9', commandDigest: 'c'.repeat(64)});
    gateway.answer(
      'Dispatch',
      encodeMessage(DispatchResponseSchema, create(DispatchResponseSchema, {receipt})),
    );
    const got = await new GatewaySource(gateway, HOST).dispatch(command, never);
    expect(got.commandId).toBe('cmd-9');
    const sent = decodeValidated(
      DispatchRequestSchema,
      gateway.unaries[0]?.request ?? new Uint8Array(),
    );
    expect(sent.command?.id).toBe('cmd-9');
  });

  test('a send the host did not answer fails as lost, so it is retried under the same id', async () => {
    const gateway = new ScriptedGateway();
    gateway.answer('Dispatch', gatewayError('gateway connection lost', 14));
    await expect(new GatewaySource(gateway, HOST).dispatch(command, never)).rejects.toMatchObject({
      kind: 'lost',
    });
  });

  test('a cancelled request is cancelled at the gateway and reported as lost', async () => {
    const gateway = new ScriptedGateway();
    gateway.answer('Dispatch', () => {
      throw new Error('never answered');
    });
    const abort = new AbortController();
    const sending = new GatewaySource(gateway, HOST).dispatch(command, abort.signal);
    abort.abort();
    await expect(sending).rejects.toMatchObject({kind: 'lost'});
    expect(gateway.unaries[0]?.cancelled).toBe(1);
  });

  test('a request that is already cancelled never reaches the gateway', async () => {
    const gateway = new ScriptedGateway();
    const abort = new AbortController();
    abort.abort();
    await expect(
      new GatewaySource(gateway, HOST).dispatch(command, abort.signal),
    ).rejects.toMatchObject({
      kind: 'lost',
    });
    expect(gateway.unaries).toEqual([]);
  });
});
