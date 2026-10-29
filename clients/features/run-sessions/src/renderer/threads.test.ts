import {expect, test} from 'bun:test';
import {create, fromBinary, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import type {HostKey} from '@vibesys/hosts';
import {FakeGatewayBridge} from '@vibesys/ipc/testing';
import {checkMachine} from '@vibesys/machine/testing';
import {
  type Record as ChatRecord,
  ChatRole,
  ChatSessionCreatedSchema,
  GetBlobsResponseSchema,
  HostCallRequestSchema,
  HostCallResponseSchema,
  ListChatSessionsResponseSchema,
  RecordSchema,
  TailRequestSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {CHAT_BLOBS_METHOD, CHAT_SESSIONS_METHOD} from '@vibesys/runs/chat';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {waitFor} from 'xstate';
import {emptyHistory, foldChat, threadsMachine} from '../state/index.js';
import {chatService, threadsActor} from './index.js';

const metadata = (id: string) =>
  create(ChatSessionCreatedSchema, {id, provider: 'claude', model: 'opus'});
const record = (seq: number, requestId: string, role: ChatRole, text: string): ChatRecord =>
  create(RecordSchema, {
    seq: BigInt(seq),
    epoch: 1,
    at: {seconds: 1n},
    body: {
      case: 'chat',
      value: {
        event: {case: 'message', value: {requestId, role, content: {case: 'text', value: text}}},
      },
    },
  });

const createdRecord = (id: string): ChatRecord =>
  create(RecordSchema, {
    seq: 1n,
    epoch: 1,
    at: {seconds: 1n},
    body: {case: 'chat', value: {event: {case: 'created', value: metadata(id)}}},
  });

const blobRecord = (seq: number, requestId: string, sha256: string): ChatRecord =>
  create(RecordSchema, {
    seq: BigInt(seq),
    epoch: 1,
    at: {seconds: 1n},
    body: {
      case: 'chat',
      value: {
        event: {
          case: 'message',
          value: {
            requestId,
            role: ChatRole.USER,
            content: {case: 'blob', value: {sha256, size: 5n}},
          },
        },
      },
    },
  });

const isMessage = (row: ChatRecord, requestId: string, role: ChatRole): boolean =>
  row.body.case === 'chat' &&
  row.body.value.event.case === 'message' &&
  row.body.value.event.value.requestId === requestId &&
  row.body.value.event.value.role === role;

/** A threads actor over a fake gateway whose logs the test owns. */
class Rig {
  readonly bridge = new FakeGatewayBridge();
  readonly clock = new FakeClock();
  readonly ports = {
    bridge: this.bridge,
    host: 'local' as HostKey,
    runId: 'run-a',
    projectRoot: '/project',
    schedule: this.clock.schedule,
  };
  readonly service = chatService(this.ports);
  readonly actor = threadsActor(this.ports);
  readonly logs = new Map<string, ChatRecord[]>();

  live() {
    return this.bridge.streams.filter(stream => !stream.cancelled);
  }

  confirmed(id: string) {
    return this.actor.getSnapshot().context.histories[id] ?? emptyHistory;
  }

  /** Send a log's records from `from` on over the newest live stream. */
  deliver(id: string, from: bigint) {
    const log = this.logs.get(id) ?? [];
    const records = log.filter(row => row.seq >= from);
    this.live()
      .at(-1)
      ?.receive(
        toBinary(
          TailResponseSchema,
          create(TailResponseSchema, {
            records: records.map(row => toBinary(RecordSchema, row)),
            throughSeq: log.at(-1)?.seq ?? 0n,
          }),
        ),
      );
  }

  async settle(id: string, through: bigint) {
    await waitFor(
      this.actor,
      snapshot => (snapshot.context.histories[id]?.throughSeq ?? 0n) >= through,
      {
        timeout: 1000,
      },
    );
  }

  async blobCall() {
    for (let attempt = 0; attempt < 50; attempt++) {
      const call = this.bridge.calls
        .filter(item => item.method === CHAT_BLOBS_METHOD && !item.cancelled)
        .at(-1);
      if (call) return call;
      await Promise.resolve();
    }
    throw new Error('Missing blob fetch');
  }
}

/** The service-side effects of create and send calls, in the order the test releases them. */
class Admissions {
  readonly pending: Array<{resolve: () => void; result: Promise<unknown>}> = [];
  readonly questions = new Map<string, string[]>();
  constructor(private readonly rig: Rig) {}

  admit(result: Promise<unknown>) {
    const call = this.rig.bridge.calls.at(-1);
    if (!call) throw new Error('Missing call');
    const method = fromBinary(HostCallRequestSchema, call.bytes).method;
    if (method.case === 'createChatSession') {
      const id = method.value.sessionId;
      if (!this.rig.logs.has(id)) {
        this.rig.logs.set(id, [createdRecord(id)]);
        this.questions.set(id, []);
      }
      this.hold(result, call.resolve, {case: 'createChatSession', value: {sessionId: id}});
    } else if (method.case === 'chatMessage') {
      const request = method.value;
      const log = this.rig.logs.get(request.sessionId);
      if (!log) throw new Error('Unknown thread');
      log.push(record(log.length + 1, request.requestId, ChatRole.USER, request.text));
      this.questions.get(request.sessionId)?.push(request.requestId);
      this.hold(result, call.resolve, {
        case: 'chatMessage',
        value: {sessionId: request.sessionId, requestId: request.requestId},
      });
    } else throw new Error('Unexpected operation');
  }

  private hold(
    result: Promise<unknown>,
    resolve: (bytes: Uint8Array) => void,
    reply: NonNullable<MessageInitShape<typeof HostCallResponseSchema>['result']>,
  ) {
    const bytes = toBinary(HostCallResponseSchema, create(HostCallResponseSchema, {result: reply}));
    this.pending.push({result, resolve: () => resolve(bytes)});
  }

  async releaseReversed() {
    for (const call of this.pending.splice(0).reverse()) {
      call.resolve();
      await call.result;
    }
  }

  /** A reply for the first question of a thread that has none yet. */
  answerOne(id: string, random: Seeded) {
    const log = this.rig.logs.get(id) as ChatRecord[];
    const unanswered = (this.questions.get(id) ?? []).filter(
      requestId => !log.some(row => isMessage(row, requestId, ChatRole.ASSISTANT)),
    );
    if (unanswered.length > 0)
      log.push(record(log.length + 1, random.pick(unanswered), ChatRole.ASSISTANT, 'answer'));
  }
}

test('generated thread selections, creation, sends, reconnects and reordered replies retain identity and release ownership', async () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    const rig = new Rig();
    const admissions = new Admissions(rig);
    const {actor, bridge, clock, logs} = rig;
    let selected: string | null = null;
    let identity = 0;
    async function listing() {
      const call = bridge.calls.at(-1);
      expect(call?.method).toBe(CHAT_SESSIONS_METHOD);
      call?.resolve(
        toBinary(
          ListChatSessionsResponseSchema,
          create(ListChatSessionsResponseSchema, {sessions: [...logs.keys()].map(metadata)}),
        ),
      );
      await waitFor(actor, snapshot => snapshot.matches({listing: 'ready'}), {timeout: 1000});
    }
    async function publish() {
      if (selected === null) return;
      const stream = rig.live().at(-1);
      if (!stream) throw new Error(`Missing session stream seed=${seed} selected=${selected}`);
      const request = fromBinary(TailRequestSchema, stream.bytes);
      expect(request.sessionId).toBe(selected);
      rig.deliver(selected, request.fromSeq);
      await rig.settle(selected, BigInt(logs.get(selected)?.length ?? 0));
      expect(rig.confirmed(selected).turns.map(turn => turn.requestId)).toEqual(
        admissions.questions.get(selected) ?? [],
      );
    }
    const operations = [
      async () => {
        const id = `thread-${identity++}`;
        const signal = new AbortController().signal;
        admissions.admit(rig.service.create(id, {provider: 'claude', model: 'opus'}, signal));
      },
      async () => {
        selected = random.bool() ? random.pick([...logs.keys()]) : null;
        actor.send({type: 'select', id: selected});
      },
      async (index: number) => {
        const text = `question-${seed}-${index}`;
        const signal = new AbortController().signal;
        admissions.admit(
          rig.service.send(random.pick([...logs.keys()]), `q-${identity++}`, text, signal),
        );
      },
      async () => {
        rig.live().at(-1)?.ended('lost');
        clock.advanceBy(1000);
      },
      async () => admissions.answerOne(random.pick([...logs.keys()]), random),
      async () => {
        await admissions.releaseReversed();
        actor.send({type: 'refresh'});
        await listing();
      },
    ];
    async function step(index: number) {
      const action = random.int(operations.length);
      const lost = action === 3 && selected === null;
      const chosen = logs.size === 0 ? 0 : lost ? operations.length - 1 : action;
      await operations[chosen]?.(index);
      await publish();
    }
    actor.start();
    try {
      await listing();
      for (let index = 0; index < 25; index++) await step(index);
      await admissions.releaseReversed();
    } finally {
      actor.stop();
    }
    expect(bridge.calls.every(call => call.cancelled)).toBe(true);
    expect(bridge.streams.every(stream => stream.cancelled)).toBe(true);
  }
});

test('generated reply completion orders preserve question order under replay', () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    const count = 2 + random.int(20);
    const ids = Array.from({length: count}, (_, index) => `q-${index}`);
    let records = ids.map((id, index) => record(index + 1, id, ChatRole.USER, id));
    const remaining = [...ids];
    while (remaining.length > 0) {
      const [id] = remaining.splice(random.int(remaining.length), 1);
      records = [
        ...records,
        record(records.length + 1, id as string, ChatRole.ASSISTANT, `answer-${id}`),
      ];
    }
    const once = foldChat(emptyHistory, {records, blobs: new Map()});
    expect(once.turns.map(turn => turn.requestId)).toEqual(ids);
    expect(foldChat(once, {records, blobs: new Map()})).toEqual(once);
  }
});

test('thread discovery and selection own every generated operation and deadline', () => {
  checkMachine({
    machine: threadsMachine,
    input: undefined,
    event: (random, live) =>
      random.pick([
        {type: 'refresh'},
        {type: 'select', id: null},
        {type: 'select', id: 'thread-a'},
        ...live.operations
          .filter(id => id === 'list')
          .map(id => live.done(id, [metadata('thread-a')])),
        ...live.operations.map(id => live.error(id, new Error('lost'))),
        ...live.delays.map(id => live.fire(id)),
      ]),
    invariants: () => [],
  });
});

async function appendText(rig: Rig, before: ReturnType<Rig['confirmed']>, index: number) {
  const log = rig.logs.get('thread-a') as ChatRecord[];
  log.push(record(log.length + 1, `q-${index}`, ChatRole.USER, `text-${index}`));
  rig.deliver('thread-a', before.throughSeq + 1n);
  await rig.settle('thread-a', BigInt(log.length));
}

/** A blob that cannot be fetched fails the history without touching what is confirmed. */
async function failBlob(rig: Rig, before: ReturnType<Rig['confirmed']>, index: number) {
  const log = rig.logs.get('thread-a') as ChatRecord[];
  log.push(blobRecord(log.length + 1, `b-${index}`, 'h'));
  rig.deliver('thread-a', before.throughSeq + 1n);
  (await rig.blobCall()).resolve(blobReply({sha256: 'h', errorCode: 5, errorMessage: 'lost'}));
  await waitFor(rig.actor, snapshot => snapshot.context.historyError !== null, {timeout: 1000});
  expect(rig.confirmed('thread-a').throughSeq).toBe(before.throughSeq);
  expect(rig.confirmed('thread-a').turns).toEqual([...before.turns]);
}

/** One generated step of a thread whose history can fail, then be retried. */
async function recoveryStep(rig: Rig, random: Seeded, index: number) {
  const failed = rig.actor.getSnapshot().context.historyError !== null;
  const before = rig.confirmed('thread-a');
  const action = random.int(4);
  if (action === 0 && !failed) await appendText(rig, before, index);
  else if (action === 1 && !failed) await failBlob(rig, before, index);
  else if (action === 2 && failed) await retry(rig, before);
  else {
    rig.live().at(-1)?.ended('lost');
    rig.clock.advanceBy(1000);
  }
  const after = rig.confirmed('thread-a');
  expect(after.throughSeq >= before.throughSeq).toBe(true);
  expect(after.turns.slice(0, before.turns.length)).toEqual([...before.turns]);
}

/** A retry keeps the confirmed messages, replaces the failed stream and resumes after them. */
async function retry(rig: Rig, before: ReturnType<Rig['confirmed']>) {
  const failedStream = rig.live().at(-1);
  rig.actor.send({type: 'reload'});
  expect(rig.actor.getSnapshot().context.historyError).toBeNull();
  expect(failedStream?.cancelled).toBe(true);
  expect(rig.live().length).toBe(1);
  const resumed = fromBinary(TailRequestSchema, (rig.live().at(-1) as {bytes: Uint8Array}).bytes);
  expect(resumed.fromSeq).toBe(before.throughSeq + 1n);
  expect(rig.confirmed('thread-a').turns).toEqual([...before.turns]);
  const log = rig.logs.get('thread-a') as ChatRecord[];
  const unread = log.filter(row => row.seq > before.throughSeq);
  rig.deliver('thread-a', resumed.fromSeq);
  if (
    unread.some(
      row =>
        row.body.case === 'chat' &&
        row.body.value.event.case === 'message' &&
        row.body.value.event.value.content.case === 'blob',
    )
  )
    (await rig.blobCall()).resolve(
      blobReply({sha256: 'h', data: new TextEncoder().encode('hello')}),
    );
  await rig.settle('thread-a', BigInt(log.length));
}

const blobReply = (
  blob: MessageInitShape<typeof GetBlobsResponseSchema>['blobs'] extends (infer B)[] | undefined
    ? B
    : never,
) => toBinary(GetBlobsResponseSchema, create(GetBlobsResponseSchema, {blobs: [blob]}));

test('generated history failures keep confirmed messages and a retry replaces the failed stream', async () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    const rig = new Rig();
    rig.logs.set('thread-a', [createdRecord('thread-a')]);
    rig.actor.start();
    rig.actor.send({type: 'select', id: 'thread-a'});
    try {
      for (let index = 0; index < 20; index++) await recoveryStep(rig, random, index);
    } finally {
      rig.actor.stop();
    }
    expect(rig.bridge.streams.every(stream => stream.cancelled)).toBe(true);
  }
});
