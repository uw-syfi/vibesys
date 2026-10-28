/** A session-log peer speaking the generated gateway RPCs used by the desktop. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  ChatRole,
  HostCallRequestSchema,
  HostCallResponseSchema,
  HostService,
  ListChatSessionsResponseSchema,
  ReadRequestSchema,
  ReadResponseSchema,
  RecordSchema,
  type Record as StoreRecord,
  TailResponseSchema,
} from '@vibesys/proto';

/** A host call the peer answers with a configuration failure instead of its result. */
export interface Refusal {
  readonly method: 'createChatSession' | 'chatMessage';
  readonly code: string;
  readonly message: string;
  readonly stderr: string;
}

export class ChatPeer {
  refuse: Refusal | undefined;
  /** The host calls the page made, by kind (`createChatSession`, `chatMessage`, ...), in order. */
  readonly hostCalls: string[] = [];
  readonly records = new Map<string, StoreRecord[]>();
  readonly watchers = new Map<string, Set<(bytes: Uint8Array) => void>>();
  constructor(bytes: number[]) {
    if (bytes.length > 0)
      this.records.set(
        'default',
        fromBinary(TailResponseSchema, new Uint8Array(bytes)).records.map(bytes =>
          fromBinary(RecordSchema, bytes),
        ),
      );
  }
  unary(method: string, bytes: Uint8Array): Uint8Array | undefined {
    const rpc = (name: keyof typeof HostService.method) =>
      `/${HostService.typeName}/${HostService.method[name].name}`;
    if (method === rpc('listChatSessions')) {
      const sessions = [...this.records.values()].flatMap(records => {
        const event = records[0]?.body.case === 'chat' ? records[0].body.value.event : undefined;
        return event?.case === 'created' ? [event.value] : [];
      });
      return toBinary(
        ListChatSessionsResponseSchema,
        create(ListChatSessionsResponseSchema, {sessions}),
      );
    }
    if (method === rpc('read')) {
      const input = fromBinary(ReadRequestSchema, bytes).method;
      if (input.case !== 'listHarnesses') return undefined;
      return toBinary(
        ReadResponseSchema,
        create(ReadResponseSchema, {
          result: {
            case: 'listHarnesses',
            value: {
              requestId: input.value.requestId,
              harnesses: [
                {provider: 'claude', model: 'opus', default: true},
                {provider: 'codex', model: 'gpt-5'},
              ],
            },
          },
        }),
      );
    }
    if (method !== rpc('hostCall')) return undefined;
    const input = fromBinary(HostCallRequestSchema, bytes).method;
    this.hostCalls.push(input.case ?? '');
    const refuse = this.refuse;
    if (refuse !== undefined && input.case === refuse.method) {
      const {code, message, stderr} = refuse;
      return toBinary(
        HostCallResponseSchema,
        create(HostCallResponseSchema, {
          result: {case: 'configurationFailed', value: {code, stage: 'chat', message, stderr}},
        }),
      );
    }
    if (input.case === 'createChatSession') {
      const request = input.value;
      if (!this.records.has(request.sessionId))
        this.records.set(request.sessionId, [
          create(RecordSchema, {
            seq: 1n,
            epoch: 1,
            at: {seconds: 1n},
            body: {
              case: 'chat',
              value: {
                event: {
                  case: 'created',
                  value: {id: request.sessionId, provider: request.provider, model: request.model},
                },
              },
            },
          }),
        ]);
      return toBinary(
        HostCallResponseSchema,
        create(HostCallResponseSchema, {
          result: {case: 'createChatSession', value: {sessionId: request.sessionId}},
        }),
      );
    }
    return undefined;
  }
  watch(id: string, from: bigint, receive: (bytes: Uint8Array) => void): () => void {
    const watchers = this.watchers.get(id) ?? new Set();
    this.watchers.set(id, watchers);
    watchers.add(receive);
    const records = (this.records.get(id) ?? []).filter(record => record.seq >= from);
    if (records.length > 0) receive(this.tail(records));
    return () => {
      watchers.delete(receive);
    };
  }
  append(bytes: number[], id = 'default'): void {
    const rows = fromBinary(TailResponseSchema, new Uint8Array(bytes)).records.map(bytes =>
      fromBinary(RecordSchema, bytes),
    );
    const history = this.records.get(id) ?? [];
    for (const row of rows)
      if (!history.some(existing => existing.seq === row.seq)) history.push(row);
    this.records.set(id, history);
    this.publish(bytes, id);
  }
  publish(bytes: number[], id = 'default'): void {
    for (const receive of this.watchers.get(id) ?? []) receive(new Uint8Array(bytes));
  }
  tail(records: readonly StoreRecord[]): Uint8Array {
    return toBinary(
      TailResponseSchema,
      create(TailResponseSchema, {
        records: records.map(record => toBinary(RecordSchema, record)),
        throughSeq: records.at(-1)?.seq ?? 0n,
      }),
    );
  }
  message(id: string, requestId: string, text: string): Uint8Array {
    const history = this.records.get(id);
    if (!history) throw new Error('Unknown chat session');
    const user = create(RecordSchema, {
      seq: BigInt(history.length + 1),
      epoch: 1,
      at: {seconds: 1n},
      body: {
        case: 'chat',
        value: {
          event: {
            case: 'message',
            value: {requestId, role: ChatRole.USER, content: {case: 'text', value: text}},
          },
        },
      },
    });
    this.append([...this.tail([user])], id);
    return toBinary(
      HostCallResponseSchema,
      create(HostCallResponseSchema, {
        result: {case: 'chatMessage', value: {sessionId: id, requestId}},
      }),
    );
  }
}
