import {expect, type Page, test} from '@playwright/test';
import type {RunEvent} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {emitChat} from './chat-events.js';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * The chat panel's threads on a live run, with no backend: New chat opens an empty thread whatever
 * the other threads hold, a thread with no title is never listed under the name of that action,
 * and the Threads menu stays inside the panel at every window width.
 */

const FIXTURE = 'queue-rs-live';
const LONG =
  'A very long thread title that goes on and on and on for quite a while, past any panel';

function chatEvent(sequence: number, type: string, thread: string, data: object, extra = {}) {
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

function created(sequence: number, thread: string, title: string): RunEvent {
  return chatEvent(sequence, 'chat_thread_created', thread, {
    thread_id: thread,
    title,
    provider: 'claude',
    model: 'opus',
    created_at: '2026-10-15T16:31:00Z',
  });
}

/** A thread with an answered question and a second answer still streaming. */
function content(from: number, thread: string, title: string): readonly RunEvent[] {
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

function firstSequence(): number {
  const events = fixtureNamed(FIXTURE).events as readonly {sequence?: number}[];
  return Math.max(...events.map(each => each.sequence ?? 0)) + 1;
}

async function open(page: Page, width: number, events: readonly RunEvent[]): Promise<void> {
  await page.setViewportSize({width, height: 900});
  const fixture = fixtureNamed(FIXTURE);
  await openFixture(page, fixture);
  await playLive(page, fixture);
  await settled(page);
  await emitChat(page, events);
  await page.getByRole('button', {name: 'Chat', exact: true}).click();
  await expect(panel(page)).toBeVisible();
  await settled(page);
}

const panel = (page: Page) => page.getByRole('region', {name: 'Chat'});

test('New chat opens an empty thread, with no transcript and nothing streaming', async ({page}) => {
  await open(page, 1440, content(firstSequence(), 'thread-a', 'Best idea so far'));
  await expect(panel(page).getByText('What is the best idea?')).toBeVisible();
  await expect(panel(page).getByText('Writing', {exact: true})).toBeVisible();
  await panel(page).getByRole('button', {name: 'New chat', exact: true}).click();
  await expect(panel(page).getByRole('combobox', {name: 'Harness'})).toBeVisible();
  await expect(panel(page).getByText('What is the best idea?')).toHaveCount(0);
  await expect(panel(page).getByText('The lock-free ring is best.')).toHaveCount(0);
  await expect(panel(page).getByText('Writing', {exact: true})).toHaveCount(0);
  await expect(panel(page).getByText('Working', {exact: true})).toHaveCount(0);
});

test('a thread with no title is never listed as New chat', async ({page}) => {
  const from = firstSequence();
  await open(page, 1440, [
    ...content(from, 'thread-a', 'Best idea so far'),
    created(from + 3, 'thread-b', ''),
  ]);
  await panel(page).getByRole('button', {name: 'Threads'}).click();
  await expect(page.getByRole('menuitem')).toHaveText(['Untitled', 'Best idea so far']);
  await expect(page.getByRole('menuitem', {name: 'New chat'})).toHaveCount(0);
});

for (const width of [1000, 1440]) {
  test(`the Threads menu stays inside the panel at ${width}px`, async ({page}) => {
    const from = firstSequence();
    await open(page, width, [
      ...content(from, 'thread-a', LONG),
      ...content(from + 3, 'thread-b', `${LONG} again`),
    ]);
    await panel(page).getByRole('button', {name: 'Threads'}).click();
    const menu = page.getByRole('menu');
    await expect(menu).toBeVisible();
    const box = await panel(page).boundingBox();
    const list = await menu.boundingBox();
    expect(box).not.toBeNull();
    expect(list).not.toBeNull();
    if (box === null || list === null) return;
    expect(list.x).toBeGreaterThanOrEqual(box.x);
    expect(list.x + list.width).toBeLessThanOrEqual(box.x + box.width);
    // A cut title still shows in full on the pointer.
    await expect(
      page.getByRole('menuitem', {name: new RegExp(LONG.slice(0, 20))}).first(),
    ).toHaveAttribute('title', /A very long thread title/);
  });
}
