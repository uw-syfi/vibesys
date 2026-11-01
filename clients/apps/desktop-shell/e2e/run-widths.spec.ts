import {expect, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {MAC_CHROME} from '../src/testing/fake-bridge.js';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * The dock lays the panel beside the screen only while it is wider than 760 px (`threads.css`). The
 * rail beside it is as wide as the window's controls (`MAC_CHROME.startWidth`), so a window shows
 * the panel beside the main from that much more.
 */
const DOCK_BESIDE_FROM = 760 + MAC_CHROME.startWidth;

/** The narrowest window that keeps the run's controls on the title's line. */
const BESIDE_FROM = 760;

/** Sweep the input space around the dock and rail breakpoints, including 660 and 1000 px. */
test('the run header stays beside its controls and chat preserves the main at dock widths', async ({
  page,
}) => {
  await openFixture(page, fixtureNamed('queue-rs-live'));
  const heading = page.getByRole('heading', {level: 1});
  const chat = page.getByRole('button', {name: 'Chat', exact: true});
  for (let width = 660; width <= 1500; width += 20) {
    await page.setViewportSize({width, height: 900});
    await expect(heading).toBeVisible();
    const title = await heading.boundingBox();
    const action = await chat.boundingBox();
    const lineHeight = await heading.evaluate(element =>
      Number.parseFloat(getComputedStyle(element).lineHeight),
    );
    expect(title?.height, `one title line at ${width}px`).toBeLessThanOrEqual(lineHeight + 1);
    expect(title).not.toBeNull();
    expect(action).not.toBeNull();
    if (!title || !action) throw new Error(`Missing header at ${width}px`);
    // A window too narrow for the title and the controls puts the controls under the title.
    if (width >= BESIDE_FROM) {
      expect(action.y, `controls stay beside the title at ${width}px`).toBeLessThan(
        title.y + title.height,
      );
      expect(action.x, `title leaves room for controls at ${width}px`).toBeGreaterThanOrEqual(
        title.x + title.width,
      );
    }
    if (width <= DOCK_BESIDE_FROM) continue;
    await chat.click();
    await expect(page.getByRole('region', {name: 'Chat'})).toBeVisible();
    await expect(heading, `main content at ${width}px`).toBeVisible();
    await page.getByRole('region', {name: 'Chat'}).getByRole('button', {name: 'Close'}).click();
  }
});

/**
 * The Ideas list fills the window below the performance row and scrolls its rows under its header:
 * six rows whole at every supported size (the compact-top spec holds nine at a laptop's size).
 */
const ROWS_WHOLE = 6;

test('the Ideas list keeps six full rows at every supported window size', async ({page}) => {
  const fixture = fixtureNamed('finished-ten-ideas');
  await openFixture(page, fixture);
  await playLive(page, fixture);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  for (const width of [660, 840, 1000, 1440]) {
    for (const height of [700, 800, 900]) {
      await page.setViewportSize({width, height});
      await settled(page);
      const row = await ideas.getByRole('option').first().boundingBox();
      const list = await ideas.boundingBox();
      expect(row, `a row at ${width}x${height}`).not.toBeNull();
      expect(
        list?.height ?? 0,
        `${ROWS_WHOLE} rows of the list at ${width}x${height}`,
      ).toBeGreaterThanOrEqual(ROWS_WHOLE * (row?.height ?? Number.POSITIVE_INFINITY));
    }
  }
});
