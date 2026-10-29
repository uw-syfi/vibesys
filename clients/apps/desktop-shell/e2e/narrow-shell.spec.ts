import {expect, type Page, test} from '@playwright/test';
import {frameProblems, STACKED_FRAME} from './checks/main-above-fold.js';
import {open, sidebar} from './world.js';

/**
 * The page is first on screen at every window width: the rail stays beside the page, at every
 * width, and never stacks above it (which put the first screen under a full-height column). The
 * check fails on the stacked frame the app used to draw.
 */

const WIDTHS = [640, 800, 1000] as const;
const HEIGHT = 700;

async function shown(page: Page, width: number): Promise<void> {
  await page.setViewportSize({width, height: HEIGHT});
  await open(page);
}

test('the frame check fails on a navigation stacked above the page', async ({page}) => {
  await page.setViewportSize({width: 640, height: HEIGHT});
  await page.setContent(`<body style="margin:0">${STACKED_FRAME}</body>`);
  expect(await frameProblems(page)).toContain('the main area starts at y=900 of a 700px window');
});

for (const width of WIDTHS) {
  test(`at ${width} px the page starts at the top, beside the rail`, async ({page}) => {
    await shown(page, width);
    expect(await frameProblems(page)).toEqual([]);
    await expect(sidebar(page)).toBeVisible();
    await expect(page.getByRole('heading', {name: 'Runs'})).toBeInViewport();
    const rail = await sidebar(page).boundingBox();
    const content = await page.locator('.vs-app-content').boundingBox();
    expect(rail?.width).toBe(64);
    expect(content?.x).toBe(64);
  });
}
