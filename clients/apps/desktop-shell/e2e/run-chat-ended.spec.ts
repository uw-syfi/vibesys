import {expect, type Page, test} from '@playwright/test';
import type {RunEvent} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {emitChat} from './chat-events.js';
import {settled} from './checks/settle.js';
import {openFixture} from './fixture-world.js';

/**
 * Chat on a run that ended, with no backend: the panel opens on the run's session logs over the
 * gateway, asks and starts chats there, and a steer the agent proposed shows as a card that cannot
 * be sent.
 */

const FIXTURE = 'crashed-after-turn';
const THREAD = 'thread-a';

interface FixtureWindow extends Window {
  fakeGateway: {calls(): string[]};
}

const calls = (page: Page) =>
  page.evaluate(() => (window as unknown as FixtureWindow).fakeGateway.calls());

function firstSequence(): number {
  const events = fixtureNamed(FIXTURE).events as readonly {sequence?: number}[];
  return Math.max(...events.map(each => each.sequence ?? 0)) + 1;
}

function event(sequence: number, type: string, fields: object): RunEvent {
  return {timestamp: '2026-10-15T16:31:00Z', type, sequence, ...fields} as unknown as RunEvent;
}

function conversation(from: number): readonly RunEvent[] {
  const chat = {agent_kind: 'chat', round_label: 'experiment-chat', chat_thread_id: THREAD};
  return [
    event(from, 'chat_thread_created', {
      ...chat,
      data: {
        kind: 'chat_thread_created',
        thread_id: THREAD,
        title: 'Why did it fail',
        provider: 'claude',
        model: 'opus',
        created_at: '2026-10-15T16:31:00Z',
      },
    }),
    event(from + 1, 'steer_proposed', {
      chat_thread_id: THREAD,
      data: {
        kind: 'steer_proposed',
        proposal_id: 'proposal-1',
        proposal: {
          text: 'Fix the engine import before the next idea.',
          target: {kind: 'role', id: 'implementer'},
          mode: 'next_turn',
        },
      },
    }),
  ];
}

const panel = (page: Page) => page.getByRole('region', {name: 'Chat'});

test.use({colorScheme: 'light', viewport: {width: 1440, height: 900}});

test('Chat opens on a failed run through the gateway, and starts no run reader', async ({page}) => {
  await openFixture(page, fixtureNamed(FIXTURE));
  const chat = page.getByRole('button', {name: 'Chat', exact: true});
  await expect(chat).toBeEnabled();
  const before = (await calls(page)).filter(method => method.endsWith('/ListChatSessions'));

  await chat.click();
  await expect(panel(page)).toBeVisible();
  await settled(page);
  // The thread list is read again on opening; nothing is attached for the run, whose log is read.
  const after = await calls(page);
  expect(after.filter(method => method.endsWith('/ListChatSessions')).length).toBeGreaterThan(
    before.length,
  );
  expect(after.filter(method => /attach|Reader/i.test(method))).toEqual([]);
});

test('a steer the agent proposed on an ended run cannot be sent', async ({page}) => {
  await openFixture(page, fixtureNamed(FIXTURE));
  await emitChat(page, conversation(firstSequence()));
  await page.getByRole('button', {name: 'Chat', exact: true}).click();
  await expect(panel(page)).toBeVisible();
  await settled(page);

  await expect(panel(page).getByRole('textbox', {name: 'Steer text'})).toHaveValue(
    'Fix the engine import before the next idea.',
  );
  await expect(panel(page).getByRole('button', {name: 'Send', exact: true})).toBeDisabled();
  await expect(panel(page).getByText('Read only').first()).toBeVisible();
});

async function openEndedChat(page: Page): Promise<void> {
  await openFixture(page, fixtureNamed(FIXTURE));
  await emitChat(page, conversation(firstSequence()));
  await page.getByRole('button', {name: 'Chat', exact: true}).click();
  await settled(page);
}

test('an ended run offers the question box and New chat, but no New steer', async ({page}) => {
  await openEndedChat(page);

  await expect(panel(page).getByText('Why did it fail')).toBeVisible();
  await expect(panel(page).getByRole('textbox', {name: 'Message'})).toHaveCount(1);
  await expect(panel(page).getByRole('button', {name: 'Ask'})).toHaveCount(1);
  await expect(panel(page).getByRole('button', {name: 'New chat'})).toHaveCount(1);
  await expect(panel(page).getByRole('button', {name: 'New steer'})).toHaveCount(0);
});
