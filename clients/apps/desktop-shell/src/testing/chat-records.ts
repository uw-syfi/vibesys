/**
 * Author the existing UI scenarios as dense native session records at the wire boundary: the data
 * half of `e2e/chat-events.ts`, which feeds them to a page, and of the DOM tests of the shell,
 * which feed them to the window's Fake gateway.
 */
import {create, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import type {RunEvent} from '@vibesys/contracts';
import {
  type ChatRecordSchema,
  ChatRole,
  RecordSchema,
  type Record as StoreRecord,
  TailResponseSchema,
} from '@vibesys/proto';
import {steerModeOf, steerTargetOf} from '@vibesys/runs/chat';

type Body = NonNullable<MessageInitShape<typeof RecordSchema>['body']>;
type ChatEvent = NonNullable<MessageInitShape<typeof ChatRecordSchema>['event']>;

const chatBody = (event: ChatEvent): Body => ({case: 'chat', value: {event}});
const messageBody = (requestId: string, role: ChatRole, text: string): Body =>
  chatBody({case: 'message', value: {requestId, role, content: {case: 'text', value: text}}});

function hasUserMessage(records: readonly StoreRecord[], requestId: string): boolean {
  return records.some(
    record =>
      record.body.case === 'chat' &&
      record.body.value.event.case === 'message' &&
      record.body.value.event.value.requestId === requestId &&
      record.body.value.event.value.role === ChatRole.USER,
  );
}

/** The chat bodies one UI event stands for, given the thread's records so far. */
/** The question a turn is asked, when the batch holds the terminal `chat` event that records it. */
function askedLater(batch: readonly RunEvent[], requestId: string): string {
  const answered = batch.find(
    each => each.data?.kind === 'chat' && each.data.invocation_id === requestId,
  );
  return answered?.text ?? '';
}

function bodiesOf(
  event: RunEvent,
  id: string,
  records: readonly StoreRecord[],
  batch: readonly RunEvent[],
): Body[] {
  const requestId =
    event.invocation_id ??
    (event.data?.kind === 'chat' ? event.data.invocation_id : null) ??
    `q-${event.sequence}`;
  const question = (text: string): Body[] =>
    hasUserMessage(records, requestId) ? [] : [messageBody(requestId, ChatRole.USER, text)];
  const data = event.data;
  if (data?.kind === 'chat_thread_created')
    return [
      chatBody({
        case: 'created',
        value: {id, title: data.title ?? '', provider: data.provider, model: data.model},
      }),
    ];
  if (data?.kind === 'agent_execution_started') return question(data.user_prompt ?? '');
  if (data?.kind === 'chat')
    return [...question(event.text ?? ''), messageBody(requestId, ChatRole.ASSISTANT, data.answer)];
  if (data?.kind === 'agent_output_chunk' && data.channel === 'assistant')
    return [
      ...question(askedLater(batch, requestId)),
      chatBody({case: 'token', value: {requestId, text: data.content}}),
    ];
  if (data?.kind === 'steer_proposed')
    return [
      chatBody({
        case: 'proposedSteer',
        value: {
          id: data.proposal_id,
          requestId,
          text: data.proposal.text,
          mode: steerModeOf(data.proposal.mode),
          target: steerTargetOf(data.proposal.target?.kind, data.proposal.target?.id),
        },
      }),
    ];
  return [];
}

/** The sessions' records so far, by thread id. */
export type ChatHistories = Map<string, StoreRecord[]>;

/**
 * The records the chat `events` add to `sessions` (which they extend), by thread: dense native
 * records, numbered after what the thread already holds.
 */
export function chatBatches(
  sessions: ChatHistories,
  events: readonly RunEvent[],
): Map<string, StoreRecord[]> {
  const updates = new Map<string, StoreRecord[]>();
  for (const event of events) {
    const id = event.chat_thread_id ?? 'default';
    const records = sessions.get(id) ?? [];
    sessions.set(id, records);
    const batch = updates.get(id) ?? [];
    updates.set(id, batch);
    for (const body of bodiesOf(event, id, records, events)) {
      const record = create(RecordSchema, {
        seq: BigInt(records.length + 1),
        epoch: 1,
        at: {seconds: 1n},
        body,
      });
      records.push(record);
      batch.push(record);
    }
  }
  return updates;
}

/** A tail reply holding `batch`, as the bytes the Fake gateway's chat peer appends. */
export function tailBytes(batch: readonly StoreRecord[]): number[] {
  return [
    ...toBinary(
      TailResponseSchema,
      create(TailResponseSchema, {
        records: batch.map(record => toBinary(RecordSchema, record)),
        throughSeq: batch.at(-1)?.seq ?? 0n,
      }),
    ),
  ];
}
