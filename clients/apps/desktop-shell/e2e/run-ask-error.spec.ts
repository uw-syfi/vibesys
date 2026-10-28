import {mkdirSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {Controls} from './chat-controls.js';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * An error handed to the run chat, with no backend: Ask about this error opens the chat on a new
 * thread and asks it about the error, in the error's own words. `SCREENS_DIR=<dir>` writes the
 * asked screen.
 */

const FIXTURE = 'crashed-after-turn';
const ASK = 'Ask about this error';
const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

interface FixtureWindow extends Window {
  fakeGateway: {hostCalls(): string[]};
}

const hostCalls = (page: Page) =>
  page.evaluate(() => (window as unknown as FixtureWindow).fakeGateway.hostCalls());
const count = (calls: readonly string[], kind: string) =>
  calls.filter(call => call === kind).length;
const panel = (page: Page) => page.getByRole('region', {name: 'Chat'});
const question = (page: Page) => panel(page).locator('.vs-chat-message[data-side="end"]');

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

test('the failed banner asks the run chat about the failure', async ({page}) => {
  const fixture = fixtureNamed(FIXTURE);
  await openFixture(page, fixture);
  const before = await hostCalls(page);
  // One error, one offer.
  const asks = page.getByRole('main').getByRole('button', {name: ASK});
  await expect(asks).toHaveCount(1);
  await asks.click();
  await expect(panel(page)).toBeVisible();
  const reason = (fixture.end?.reason ?? '').slice(0, 80);
  expect(reason).not.toBe('');
  await expect(question(page)).toContainText('VibeSys app (Run ended: Failed):');
  await expect(question(page)).toContainText(reason);
  const after = await hostCalls(page);
  expect(count(after, 'createChatSession') - count(before, 'createChatSession')).toBe(1);
  expect(count(after, 'chatMessage') - count(before, 'chatMessage')).toBe(1);
  await settled(page);
  if (shots !== undefined) await page.screenshot({path: `${shots}/run-ask__asked.png`});
});

test('a question the host refused offers its details to copy, not a question that would fail too', async ({
  page,
}) => {
  const fixture = fixtureNamed(FIXTURE);
  const controls = new Controls(page, fixture);
  await openFixture(page, fixture);
  await page.getByRole('button', {name: 'Chat', exact: true}).click();
  await expect(panel(page)).toBeVisible();
  await controls.refuse({
    method: 'chatMessage',
    code: 'chat_unavailable',
    message: 'Chat is not available for this run.',
    stderr: 'agent exited with status 2',
  });
  await panel(page).getByRole('textbox', {name: 'Message'}).fill('Why?');
  await panel(page).getByRole('button', {name: 'Ask', exact: true}).click();
  await expect(panel(page).getByText('Chat is not available for this run.')).toBeVisible();
  await expect(panel(page).getByRole('button', {name: 'Copy details'})).toBeVisible();
  await expect(panel(page).getByRole('button', {name: ASK})).toHaveCount(0);
  await expect(panel(page).getByRole('textbox', {name: 'Message'})).toHaveValue('Why?');
  if (shots !== undefined) await page.screenshot({path: `${shots}/run-ask__chat-refusal.png`});
});

test('a request the run did not take asks the run chat about it', async ({page}) => {
  const fixture = fixtureNamed('queue-rs-live');
  await openFixture(page, fixture, {refuseActions: ['pause']});
  await playLive(page, fixture);
  await page.getByRole('button', {name: 'Pause', exact: true}).click();
  await expect(page.getByText('Pause · Not confirmed')).toBeVisible();
  // The reason shows once, under its request, with one offer to ask about it.
  await expect(page.getByText('The run did not take the command', {exact: true})).toHaveCount(1);
  await expect(page.getByText('Updates unavailable')).toHaveCount(0);
  await page.getByRole('main').getByRole('button', {name: ASK}).click();
  await expect(question(page)).toContainText('Pause request');
  await expect(question(page)).toContainText('The run did not take the command');
  if (shots !== undefined) await page.screenshot({path: `${shots}/run-ask__request-refusal.png`});
});
