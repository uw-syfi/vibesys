/**
 * Chat threads as the run's events tell them (`chat-records.ts` makes the native records of
 * them): a thread created, a question answered, an answer still streaming, a steer the agent
 * proposed. The data half of the run-chat journeys, for the browser specs (`e2e/run-chat-*.spec.ts`)
 * and the DOM tests of the shell (`run-chat.dom.test.tsx`).
 */
import type {RunEvent} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';

/** The sequence after the last event the fixture's own run holds: where a chat's events begin. */
export function sequenceAfter(fixture: string): number {
  const events = fixtureNamed(fixture).events as readonly {sequence?: number}[];
  return Math.max(...events.map(each => each.sequence ?? 0)) + 1;
}

function chatEvent(
  sequence: number,
  type: string,
  thread: string,
  data: object,
  extra = {},
): RunEvent {
  return {
    timestamp: '2026-10-15T16:31:00Z',
    type,
    sequence,
    agent_kind: 'chat',
    round_label: 'experiment-chat',
    chat_thread_id: thread,
    data: {kind: type, ...data},
    ...extra,
  } as unknown as RunEvent;
}

export function created(sequence: number, thread: string, title: string): RunEvent {
  return chatEvent(sequence, 'chat_thread_created', thread, {
    thread_id: thread,
    title,
    provider: 'claude',
    model: 'opus',
    created_at: '2026-10-15T16:31:00Z',
  });
}

/** A thread with an answered question and a second answer still streaming. */
export function content(from: number, thread: string, title: string): readonly RunEvent[] {
  return [
    created(from, thread, title),
    chatEvent(
      from + 1,
      'chat',
      thread,
      {answer: 'The lock-free ring is best.', invocation_id: `turn-${from}`},
      {text: 'What is the best idea?'},
    ),
    chatEvent(
      from + 2,
      'agent_output_chunk',
      thread,
      {channel: 'assistant', content: 'Still writing the second answer'},
      {invocation_id: `turn-${from + 2}`},
    ),
  ];
}

/** A thread whose agent proposed a steer. */
export function proposal(from: number, thread: string, title: string): readonly RunEvent[] {
  return [
    created(from, thread, title),
    chatEvent(from + 1, 'steer_proposed', thread, {
      proposal_id: 'proposal-1',
      proposal: {
        text: 'Fix the engine import before the next idea.',
        target: {kind: 'role', id: 'implementer'},
        mode: 'next_turn',
      },
    }),
  ];
}

export const QUESTION =
  'What is the best measured result of this run so far? Read-only question, change nothing.';
export const ANSWER = 'The best idea is the lock-free ring.';

/** One chat turn as a real backend records it: the answer arrives in the terminal `chat` event, so the question and the answer share its sequence. */
export function turn(from: number, thread: string): readonly RunEvent[] {
  const execution = {execution_id: 'exec-chat-1', invocation_id: 'exec-chat-1'};
  return [
    created(from, thread, 'Best idea'),
    chatEvent(
      from + 1,
      'agent_execution_started',
      thread,
      {
        stage: 'chat',
        user_prompt: QUESTION,
        activity: {kind: 'agent_execution_activity_changed', mode: 'thinking', summary: 'Thinking'},
        provider: 'claude',
        model: 'claude-sonnet-5-5',
      },
      execution,
    ),
    chatEvent(
      from + 2,
      'chat',
      thread,
      {answer: ANSWER, invocation_id: 'exec-chat-1'},
      {...execution, text: QUESTION, status: 'answered'},
    ),
    chatEvent(
      from + 3,
      'turn_usage_recorded',
      thread,
      {input_tokens: 10, output_tokens: 5, duration_ms: 900},
      execution,
    ),
    chatEvent(
      from + 4,
      'agent_execution_finished',
      thread,
      {result: ANSWER},
      {...execution, status: 'completed'},
    ),
  ];
}
