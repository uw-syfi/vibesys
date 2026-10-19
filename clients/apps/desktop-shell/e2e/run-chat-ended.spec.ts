import {expect, type Page, test} from '@playwright/test';
import type {RunEvent} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {fixtureScripts, openFixture} from './fixture-world.js';

/**
 * Chat on a run that ended, with no backend: the run's page attaches the reader the host starts for
 * it, the panel opens on that server's session, and a steer the agent proposed shows as a card
 * that cannot be sent.
 */

const FIXTURE = 'crashed-after-turn';
const THREAD = 'thread-a';

interface FixtureWindow extends Window {
  fakeHost: {emit(events: readonly unknown[]): void; readerAttaches(): number};
}

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

test('Chat opens on a failed run through its reader, once', async ({page}) => {
  await openFixture(page, fixtureNamed(FIXTURE));
  const chat = page.getByRole('button', {name: 'Chat', exact: true});
  await expect(chat).toBeEnabled();
  expect(
    await page.evaluate(() => (window as unknown as FixtureWindow).fakeHost.readerAttaches()),
  ).toBe(0);

  await chat.click();
  await expect(panel(page)).toBeVisible();
  await settled(page);
  expect(
    await page.evaluate(() => (window as unknown as FixtureWindow).fakeHost.readerAttaches()),
  ).toBe(1);
});

test('a steer the agent proposed on an ended run cannot be sent', async ({page}) => {
  await openFixture(page, fixtureNamed(FIXTURE));
  await page.getByRole('button', {name: 'Chat', exact: true}).click();
  await expect(panel(page)).toBeVisible();
  await settled(page);
  await page.evaluate(
    events => (window as unknown as FixtureWindow).fakeHost.emit(events),
    conversation(firstSequence()) as readonly unknown[],
  );
  await settled(page);

  await expect(panel(page).getByRole('textbox', {name: 'Steer text'})).toHaveValue(
    'Fix the engine import before the next idea.',
  );
  await expect(panel(page).getByRole('button', {name: 'Send', exact: true})).toBeDisabled();
  await expect(panel(page).getByText('Read only').first()).toBeVisible();
});

test('the panel on an ended run reads its threads and offers nothing to ask', async ({page}) => {
  await openFixture(page, fixtureNamed(FIXTURE));
  await page.getByRole('button', {name: 'Chat', exact: true}).click();
  await settled(page);
  await page.evaluate(
    events => (window as unknown as FixtureWindow).fakeHost.emit(events),
    conversation(firstSequence()) as readonly unknown[],
  );
  await settled(page);

  await expect(panel(page).getByText('Why did it fail')).toBeVisible();
  await expect(panel(page).getByRole('textbox', {name: 'Message'})).toHaveCount(0);
  await expect(panel(page).getByRole('button', {name: 'Ask'})).toHaveCount(0);
  await expect(panel(page).getByRole('button', {name: 'New chat'})).toHaveCount(0);
  await expect(panel(page).getByRole('button', {name: 'New steer'})).toHaveCount(0);
  await expect(panel(page).getByText('Read only', {exact: true}).first()).toBeVisible();
});

test('an older helper shows Chat off for an ended run, and nothing is attached', async ({page}) => {
  // The world's scripts first, then a helper that offers none of the features.
  for (const content of fixtureScripts(fixtureNamed(FIXTURE))) await page.addInitScript({content});
  await page.addInitScript({
    content:
      'Object.assign(window.vibesysWelcome, {helperFeatures: () => Promise.resolve({ok: true, value: []})});',
  });
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .first()
    .click();
  await expect(page.getByRole('button', {name: 'Chat', exact: true})).toBeDisabled();
  await expect(page.getByText('Needs an update')).toBeVisible();
  expect(
    await page.evaluate(() => (window as unknown as FixtureWindow).fakeHost.readerAttaches()),
  ).toBe(0);
});
