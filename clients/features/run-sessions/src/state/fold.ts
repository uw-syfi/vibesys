import type {ChatMessage} from '@vibesys/runs/chat';
import {ChatRole, type ChatSteerProposed, type Record} from '@vibesys/runs/chat';

export interface ChatTurn {
  readonly requestId: string;
  readonly question: string;
  readonly answer: string;
  readonly streaming: boolean;
  readonly failed: boolean;
}
export interface ChatHistory {
  readonly throughSeq: bigint;
  readonly turns: readonly ChatTurn[];
  readonly proposals: readonly ChatSteerProposed[];
}
export const emptyHistory: ChatHistory = {throughSeq: 0n, turns: [], proposals: []};
export interface ChatBatch {
  readonly records: readonly Record[];
  readonly blobs: ReadonlyMap<string, string>;
}

/** Replayed records are idempotent; a restarted user record resets abandoned streaming. */
export function foldChat(history: ChatHistory, batch: ChatBatch): ChatHistory {
  let state = history;
  for (const record of batch.records) {
    if (record.seq <= state.throughSeq) continue;
    if (record.seq !== state.throughSeq + 1n) throw new Error('Chat history gap');
    state = {...state, throughSeq: record.seq};
    state = foldRecord(state, record, batch.blobs);
  }
  return state;
}

function foldRecord(
  state: ChatHistory,
  record: Record,
  blobs: ReadonlyMap<string, string>,
): ChatHistory {
  if (record.body.case !== 'chat') return state;
  const event = record.body.value.event;
  if (event.case === 'message') {
    return foldMessage(state, event.value, blobs);
  } else if (event.case === 'token') {
    state = {
      ...state,
      turns: state.turns.map(turn =>
        turn.requestId === event.value.requestId && turn.streaming
          ? {...turn, answer: turn.answer + event.value.text}
          : turn,
      ),
    };
  } else if (
    event.case === 'proposedSteer' &&
    !state.proposals.some(proposal => proposal.id === event.value.id)
  ) {
    state = {...state, proposals: [...state.proposals, event.value]};
  }
  return state;
}

function foldMessage(
  state: ChatHistory,
  message: ChatMessage,
  blobs: ReadonlyMap<string, string>,
): ChatHistory {
  const text =
    message.content.case === 'text'
      ? message.content.value
      : message.content.case === 'blob'
        ? blobs.get(message.content.value.sha256)
        : undefined;
  if (text === undefined) throw new Error('Chat message content unavailable');
  const previous = state.turns.find(turn => turn.requestId === message.requestId);
  const turn: ChatTurn =
    message.role === ChatRole.USER
      ? {
          requestId: message.requestId,
          question: text,
          answer: '',
          streaming: true,
          failed: false,
        }
      : {
          ...(previous ?? {requestId: message.requestId, question: ''}),
          answer: text,
          streaming: false,
          failed: message.failed,
        };
  state = {
    ...state,
    turns: previous
      ? state.turns.map(item => (item.requestId === turn.requestId ? turn : item))
      : [...state.turns, turn],
  };
  return state;
}
