import {expect, type Page, test} from '@playwright/test';
import type {RunEvent} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {content, sequenceAfter} from '../src/testing/chat-scenes.js';
import {emitChat} from './chat-events.js';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * Where the chat panel's Threads menu sits on a live run, with no backend: inside the panel at every
 * window width, a cut title whole on the pointer. What the threads say (New chat opening an empty
 * thread, an untitled thread never listed as that action) is `src/run-chat.dom.test.tsx`.
 */

const FIXTURE = 'queue-rs-live';
const LONG =
  'A very long thread title that goes on and on and on for quite a while, past any panel';

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

for (const width of [1000, 1440]) {
  test(`the Threads menu stays inside the panel at ${width}px`, async ({page}) => {
    const from = sequenceAfter(FIXTURE);
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
