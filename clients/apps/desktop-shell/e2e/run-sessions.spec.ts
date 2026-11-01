/**
 * The layout of the run's chat, app window only: its history, the answer streaming in and answered,
 * the history unavailable and the hint its chip draws, and a parked and a stopped run's history,
 * each on one page resized across the widths. What the chat says and posts, and that it recovers
 * its history, is `src/run-sessions.dom.test.tsx`. `SCREENS_DIR` writes each shot.
 */
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {create} from '@bufbuild/protobuf';
import {expect, type Locator, type Page, test} from '@playwright/test';
import {RecordSchema} from '@vibesys/proto';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {chatTail, exchange, sessionWorld} from '../src/testing/sessions-world.js';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {installGateway} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

interface GatewayWindow {
  fakeGateway: {appendChat(bytes: number[]): void; sendChatFrame(bytes: number[]): void};
}
const shots = process.env['SCREENS_DIR'];
const sheet: SheetEntry[] = [];
const WIDTHS = [1000, 1440, 1920];
const HISTORY = 'The queue avoids repeated allocation.';

async function openSession(
  page: Page,
  state: 'live' | 'parked' | 'stopped',
): Promise<{pane: Locator; world: ReturnType<typeof sessionWorld>}> {
  await page.setViewportSize({width: WIDTHS[0] ?? 1000, height: 900});
  const world = sessionWorld(state);
  await page.exposeFunction('postIntent', (host: string, bytes: number[]) =>
    world.postIntent(host, bytes),
  );
  await installGateway(page, world.input);
  await openFixture(
    page,
    state === 'live'
      ? {...fixtureNamed('queue-rs-live'), runId: 'run-a'}
      : {...fixtureNamed('finished-run'), runId: 'run-a'},
  );
  await page.getByRole('button', {name: 'Chat', exact: true}).click();
  const pane = page.getByRole('region', {name: 'Chat', exact: true}).last();
  await expect(pane.getByText(HISTORY, {exact: true})).toBeVisible();
  return {pane, world};
}

/** Lays the chat out in the state it is in at every width, on the one page. */
async function captureAtEveryWidth(page: Page, state: string): Promise<void> {
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    await captureSession(page, width, state);
  }
}

test('the chat lays out at every width through its history, its streaming answer and an unavailable history', async ({
  page,
}) => {
  const {pane, world} = await openSession(page, 'live');
  await captureAtEveryWidth(page, 'history');
  await pane.getByRole('button', {name: 'Send', exact: true}).click();
  await expect(pane.getByText('Sent', {exact: true})).toBeAttached();
  await pane.getByRole('textbox', {name: 'Message'}).fill('Why did it win?');
  await pane.getByRole('button', {name: 'Ask', exact: true}).click();
  // The window's question is the second call to the host, named by its request id.
  await expect.poll(() => world.requests.length).toBe(2);
  const method = world.requests[1]?.method;
  if (method?.case !== 'chatMessage') throw new Error('Expected chat-message');
  const {question, token, answer} = exchange(method.value.requestId, method.value.text);
  await page.evaluate(
    bytes => (window as unknown as GatewayWindow).fakeGateway.appendChat(bytes),
    chatTail([question, token]),
  );
  await expect(pane.getByText('It reduces contention', {exact: true})).toBeVisible();
  await captureAtEveryWidth(page, 'streaming');
  await page.evaluate(
    bytes => (window as unknown as GatewayWindow).fakeGateway.appendChat(bytes),
    chatTail([answer]),
  );
  await expect(pane.getByText('It reduces contention under load.', {exact: true})).toBeVisible();
  await captureAtEveryWidth(page, 'answered');
  await page.evaluate(
    bytes => (window as unknown as GatewayWindow).fakeGateway.sendChatFrame(bytes),
    chatTail([create(RecordSchema, {...answer, seq: 10n})]),
  );
  await expect(pane.getByText('Unavailable', {exact: true})).toBeVisible();
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    await captureSession(page, width, 'unavailable');
    await hintCoversNoControl(page, pane, width);
  }
});

for (const state of ['parked', 'stopped'] as const)
  test(`a ${state} run's chat history lays out at every width`, async ({page}) => {
    await openSession(page, state);
    await captureAtEveryWidth(page, `${state}-history`);
  });

test('session app-window contact sheet', async ({page}) => {
  if (!shots || sheet.length === 0) return;
  const html = contactSheetHtml(sheet, 3);
  writeFileSync(join(shots, 'session-contact-sheet.html'), html);
  await page.goto(`file://${join(shots, 'session-contact-sheet.html')}`);
  await page.setViewportSize({width: 1080, height: 900});
  await page.screenshot({path: join(shots, 'session-contact-sheet.png'), fullPage: true});
});

async function captureSession(page: Page, width: number, state: string): Promise<void> {
  await settled(page);
  expect(await screenCheck(page)).toEqual([]);
  if (!shots) return;
  mkdirSync(shots, {recursive: true});
  const file = `session-${width}-${state}.png`;
  await page.screenshot({path: join(shots, file)});
  sheet.push({name: `${width}/${state}`, file});
}

/**
 * The chip that says the history is unavailable draws its hint under itself while it has the keyboard
 * focus. Over what? The chat's title row holds Threads, New chat and Close: the hint covers none of
 * them, with the words the chat shows and with a hint as long as the browser lets it grow (a raw
 * error of the thread list). `focus` is the keyboard's (Shift+Tab, Tab), which is what shows the hint.
 */
async function hintCoversNoControl(page: Page, pane: Locator, width: number): Promise<void> {
  const chip = pane.locator('.vs-chip[aria-description]', {hasText: 'Unavailable'});
  const check = async (state: string): Promise<void> => {
    await chip.focus();
    await page.keyboard.press('Shift+Tab');
    await page.keyboard.press('Tab');
    // The hint is a pseudo-element anchored under its chip: its box is what the browser resolved.
    const hint = await chip.evaluate(each => {
      const drawn = getComputedStyle(each, '::after');
      const left = Number.parseFloat(drawn.left);
      const top = Number.parseFloat(drawn.top);
      return {
        shown: drawn.content !== 'none',
        left,
        top,
        right: left + Number.parseFloat(drawn.width),
        bottom: top + Number.parseFloat(drawn.height),
      };
    });
    expect(hint.shown, 'keyboard focus draws the hint').toBe(true);
    for (const name of ['Threads', 'New chat', 'Close']) {
      const box = await pane.getByRole('button', {name, exact: true}).boundingBox();
      if (box === null) throw new Error(`the chat has no ${name} button`);
      const apart =
        hint.right <= box.x ||
        box.x + box.width <= hint.left ||
        hint.bottom <= box.y ||
        box.y + box.height <= hint.top;
      expect(apart, `the ${state} hint covers ${name} at ${width}px`).toBe(true);
    }
    await captureSession(page, width, `unavailable-hint-${state}`);
    await page.keyboard.press('Tab');
  };
  await check('short');
  await chip.evaluate(each =>
    each.setAttribute('aria-description', 'The thread list could not be read: '.repeat(8)),
  );
  await check('long');
}
