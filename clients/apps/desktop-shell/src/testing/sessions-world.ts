/**
 * A run's chat as the host holds it: a session whose history replays, a host that admits a steer
 * and a question, and the records of an answer that streams in. The data half of the run-sessions
 * journeys, for the browser spec (`e2e/run-sessions.spec.ts`) and the DOM tests of the shell
 * (`run-sessions.dom.test.tsx`).
 */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  ChatRole,
  type HostCallRequest,
  HostCallRequestSchema,
  HostCallResponseSchema,
  LifecycleSchema,
  Outcome,
  RecordSchema,
  type Record as StoreRecord,
  TailResponseSchema,
} from '@vibesys/proto';
import {chatReplay, storeRunFixture} from '@vibesys/run-fixtures';
import {type WireFixture, wire} from './gateway-fixture.js';

/** A tail reply holding `records`, as the bytes the Fake gateway's chat peer appends. */
export const chatTail = (records: readonly StoreRecord[]): number[] => [
  ...toBinary(
    TailResponseSchema,
    create(TailResponseSchema, {records: records.map(record => toBinary(RecordSchema, record))}),
  ),
];

export interface SessionWorld {
  /** What the Fake gateway serves: the run, with its chat session's history. */
  readonly input: WireFixture;
  /** Every call the window made to the host, in order. */
  readonly requests: HostCallRequest[];
  readonly postIntent: (host: string, bytes: number[]) => number[];
}

/** A run in `state` whose chat session already holds a replayed exchange, over a host that admits what the window posts. */
export function sessionWorld(state: 'live' | 'parked' | 'stopped' = 'live'): SessionWorld {
  const source = storeRunFixture(state === 'live' ? 'live' : 'short');
  if (state !== 'live') {
    const last = source.records.at(-1);
    if (last?.body.case !== 'lifecycle') throw new Error('Missing lifecycle');
    last.body.value = create(LifecycleSchema, {
      state:
        state === 'parked'
          ? {case: 'parked', value: {}}
          : {case: 'ended', value: {outcome: Outcome.STOPPED}},
    });
  }
  const input = wire(source);
  input.chat = chatTail(chatReplay());
  const requests: HostCallRequest[] = [];
  return {
    input,
    requests,
    postIntent: (_host, bytes) => {
      const envelope = fromBinary(HostCallRequestSchema, new Uint8Array(bytes));
      requests.push(envelope);
      const result =
        envelope.method.case === 'chatMessage'
          ? {
              case: 'chatMessage' as const,
              value: {
                requestId: envelope.method.value.requestId,
                sessionId: envelope.method.value.sessionId,
              },
            }
          : {
              case: 'postIntent' as const,
              value: {
                requestId:
                  envelope.method.case === 'postIntent' ? envelope.method.value.requestId : '',
                orderedReader: true,
              },
            };
      return [...toBinary(HostCallResponseSchema, create(HostCallResponseSchema, {result}))];
    },
  };
}

/** The records of a question the window asked and the answer that streams in after it. */
export function exchange(
  requestId: string,
  text: string,
): {question: StoreRecord; token: StoreRecord; answer: StoreRecord} {
  const chat = (seq: bigint, seconds: bigint, event: object): StoreRecord =>
    create(RecordSchema, {
      seq,
      epoch: 2,
      at: {seconds},
      body: {case: 'chat', value: {event}},
    } as never);
  return {
    question: chat(6n, 1700000100n, {
      case: 'message',
      value: {requestId, role: ChatRole.USER, content: {case: 'text', value: text}},
    }),
    token: chat(7n, 1700000101n, {
      case: 'token',
      value: {requestId, text: 'It reduces contention'},
    }),
    answer: chat(8n, 1700000102n, {
      case: 'message',
      value: {
        requestId,
        role: ChatRole.ASSISTANT,
        content: {case: 'text', value: 'It reduces contention under load.'},
      },
    }),
  };
}
