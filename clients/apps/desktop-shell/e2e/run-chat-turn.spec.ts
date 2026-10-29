import {expect, type Page, test} from '@playwright/test';
import type {RunEvent} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {emitChat} from './chat-events.js';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * A chat turn as a real backend records it, on a live run with no backend of its own: the answer
 * arrives in the terminal `chat` event with nothing streamed before it, so the question and the
 * answer carry the same event's sequence. Opening Chat on that thread must draw it, with the page
 * raising no error.
 */

const FIXTURE = 'queue-rs-live';
const THREAD = 'thread-a';
const EXECUTION = 'exec-chat-1';
const QUESTION =
  'What is the best measured result of this run so far? Read-only question, change nothing.';
const ANSWER = 'The best idea is the lock-free ring.';
const CHAT = {agent_kind: 'chat', round_label: 'experiment-chat', chat_thread_id: THREAD} as const;

function event(sequence: number, type: string, fields: object = {}): RunEvent {
  return {timestamp: '2026-10-15T16:31:00Z', type, sequence, ...fields} as unknown as RunEvent;
}

/** One chat turn's events, from creating the thread to the execution finishing. */
function turn(from: number): readonly RunEvent[] {
  const execution = {execution_id: EXECUTION, invocation_id: EXECUTION};
  return [
    event(from, 'chat_thread_created', {
      ...CHAT,
      data: {
        kind: 'chat_thread_created',
        thread_id: THREAD,
        title: 'Best idea',
        provider: 'claude',
        model: 'claude-sonnet-5-5',
        created_at: '2026-10-15T16:31:00Z',
      },
    }),
    event(from + 1, 'agent_execution_started', {
      ...CHAT,
      ...execution,
      data: {
        kind: 'agent_execution_started',
        stage: 'chat',
        user_prompt: QUESTION,
        activity: {kind: 'agent_execution_activity_changed', mode: 'thinking', summary: 'Thinking'},
        provider: 'claude',
        model: 'claude-sonnet-5-5',
      },
    }),
    event(from + 2, 'chat', {
      ...CHAT,
      ...execution,
      text: QUESTION,
      status: 'answered',
      data: {kind: 'chat', answer: ANSWER, invocation_id: EXECUTION},
    }),
    event(from + 3, 'turn_usage_recorded', {
      ...CHAT,
      ...execution,
      data: {kind: 'turn_usage_recorded', input_tokens: 10, output_tokens: 5, duration_ms: 900},
    }),
    event(from + 4, 'agent_execution_finished', {
      ...CHAT,
      ...execution,
      status: 'completed',
      data: {kind: 'agent_execution_finished', result: ANSWER},
    }),
  ];
}

async function emit(page: Page, events: readonly RunEvent[]): Promise<void> {
  await emitChat(page, events);
}

test('Chat opens on a run whose thread holds a question and an answer, and settles', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({width: 1440, height: 900});
  const fixture = fixtureNamed(FIXTURE);
  await openFixture(page, fixture);
  await playLive(page, fixture);
  await settled(page);
  const events = fixture.events as readonly {sequence?: number}[];
  const from = Math.max(...events.map(each => each.sequence ?? 0)) + 1;
  await emit(page, turn(from));
  await settled(page);
  await page.getByRole('button', {name: 'Chat', exact: true}).click();
  await settled(page);
  const panel = page.getByRole('region', {name: 'Chat'});
  await expect(panel.getByText(QUESTION)).toBeVisible();
  await expect(panel.getByText(ANSWER)).toBeVisible();
  await expect(panel.getByText('Chat failed')).toHaveCount(0);
  await expect(page.getByRole('heading', {level: 1})).toBeVisible();
  expect(errors).toEqual([]);
  // Reopening shows the same thread.
  await panel.getByRole('button', {name: 'Close'}).click();
  await page.getByRole('button', {name: 'Chat', exact: true}).click();
  await expect(panel.getByText(ANSWER)).toBeVisible();
  expect(errors).toEqual([]);
});
