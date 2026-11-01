import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {openFixture} from './fixture-world.js';

/**
 * A finished run of ten ideas (six passed, four failed), as the user sees it: the failed ideas are
 * listed by default in the flat table and can be hidden by the filter; the performance row
 * is a chart per objective with a headline and a unit on its values; the chart joins no dots; and tokens are split
 * into input (with its cached share) and output wherever they are written, with a part nobody
 * reported written as unknown. `RUN_SHOTS` names a directory to save the app window in.
 */

const shots = process.env['RUN_SHOTS'];

async function shot(page: Page, name: string): Promise<void> {
  if (shots === undefined) return;
  const width = page.viewportSize()?.width ?? 0;
  await page.screenshot({path: `${shots}/${name}-${width}.png`});
}

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

const ideas = (page: Page) => page.getByRole('listbox', {name: 'Ideas'});
const failedChip = (page: Page) =>
  page.locator('button.vs-dotlegend-item', {hasText: /^Failed \d+/});
const failedRows = (page: Page) =>
  ideas(page)
    .getByRole('option')
    .filter({has: page.locator('[role="img"][aria-label="Failed"]')});

test('failed ideas are listed in the flat table and a chip lists only them', async ({page}) => {
  await openFixture(page, fixtureNamed('finished-ten-ideas'));
  await expect(ideas(page).locator('.vs-guide, .vs-pickrow-fold')).toHaveCount(0);
  await expect(ideas(page).getByRole('option')).toHaveCount(10);
  await expect(failedRows(page)).toHaveCount(3);
  await expect(failedChip(page)).toHaveAttribute('aria-pressed', 'false');
  // A failed idea keeps its cause visible under its status.
  await expect(ideas(page).getByRole('option', {name: /^Replace the mutex/})).toContainText(
    /review rejected/i,
  );
  for (const width of [1440, 1000]) {
    await page.setViewportSize({width, height: 2000});
    const visibility = await failedRows(page).evaluateAll(rows =>
      rows.map(row => {
        const reason = row.querySelector('.vs-columnlist-lines > .vs-text');
        const box = reason?.getBoundingClientRect();
        const parent = reason?.closest('.vs-columnlist-cell')?.getBoundingClientRect();
        return {
          clipped: reason !== null && reason.scrollWidth > reason.clientWidth + 1,
          inRow: box !== undefined && parent !== undefined && box.right <= parent.right + 1,
        };
      }),
    );
    expect(visibility).toHaveLength(3);
    expect(visibility.every(item => !item.clipped && item.inRow)).toBe(true);
  }
  await shot(page, 'ten-ideas-table');

  await failedChip(page).click();
  await expect(failedRows(page)).toHaveCount(3);
  await expect(ideas(page).getByRole('option')).toHaveCount(3);
  await expect(failedChip(page)).toHaveAttribute('aria-pressed', 'true');
  await failedChip(page).click();
  await expect(ideas(page).getByRole('option')).toHaveCount(10);
});

test('the performance row is a chart with a headline and values that carry a unit', async ({
  page,
}) => {
  await openFixture(page, fixtureNamed('finished-ten-ideas'));
  const panel = page.getByRole('region', {name: 'Total ops/sec', exact: true});
  await expect(panel).toBeVisible();
  await expect(panel.locator('.vs-chart-figure')).toHaveText('3.83× faster');
  await expect(panel.locator('.vs-chart-figure')).toHaveAccessibleDescription(
    '6,054,154 ops/s · input 1,581,539 ops/s',
  );
  // No line names the idea the figures belong to: the Ideas list badges it.
  await expect(page.getByText(/Adopted:/)).toHaveCount(0);
  // The key names what the chart draws, and the ring of an open idea only while one is open.
  const key = page.getByRole('list', {name: 'Chart key'});
  await expect(key.getByText('Best so far', {exact: true})).toBeVisible();
  await expect(key.getByText('Reading', {exact: true})).toBeVisible();
  await expect(key.getByText('Selected', {exact: true})).toHaveCount(0);
  await shot(page, 'ten-ideas-hero');
});

test('the chart draws the best-so-far step line and joins no dots', async ({page}) => {
  await openFixture(page, fixtureNamed('finished-ten-ideas'));
  const chart = page.getByRole('group', {name: /of each reading over the run/});
  await expect(chart.locator('.vs-chart-point').first()).toBeVisible();
  await expect(chart.locator('path')).toHaveCount(1);
  await expect(chart.locator('path.vs-chart-step')).toHaveCount(1);
  await expect(chart.locator('.vs-chart-edge, .vs-chart-edges')).toHaveCount(0);
  await ideas(page).getByRole('option').first().hover();
  await expect(chart.locator('.vs-chart-lit').first()).toBeVisible();
  await expect(chart.locator('path')).toHaveCount(1);
  await shot(page, 'ten-ideas-chart');
});

test('tokens are split into input with its cached share, and output, where they are written', async ({
  page,
}) => {
  await openFixture(page, fixtureNamed('finished-ten-ideas'));
  // The stats row: input, its cached share, then output; the cached share is a lower bound because
  // one turn reported no cache counts.
  await expect(page.locator('.vs-stat', {hasText: 'Tokens'})).toContainText(
    /8\.1M input\s*· 6\.5M cached · 459k output/,
  );
  await expect(page.getByText(/tokens/)).toHaveCount(0);

  await page.getByRole('radio', {name: 'Agents'}).check({force: true});
  const agents = page.getByRole('listbox', {name: 'Agents'});
  await expect(page.getByText('in (cached)', {exact: true})).toBeVisible();
  await expect(page.getByText('out', {exact: true})).toBeVisible();
  const cells = await agents
    .getByRole('option')
    .evaluateAll(rows =>
      rows.map(row =>
        [...row.querySelectorAll('.vs-pickrow-measure')].map(cell => cell.textContent ?? ''),
      ),
    );
  const flat = cells.flat();
  expect(flat.some(cell => /^\d+(\.\d)?[kM] \d+(\.\d)?[kM] cached$/.test(cell))).toBe(true);
  // The turn without cache counts says so, and the turn that reported nothing says unknown.
  expect(flat.some(cell => cell.includes('cached unknown'))).toBe(true);
  expect(flat).toContain('unknown');
  expect(flat).not.toContain('0');
  await shot(page, 'ten-ideas-agents');
});

test('the hero, the chart and the ideas table at 1440 and 1000 px', async ({page}) => {
  test.skip(shots === undefined, 'set RUN_SHOTS to save the app window');
  await openFixture(page, fixtureNamed('finished-ten-ideas'));
  for (const width of [1440, 1000]) {
    await page.setViewportSize({width, height: 900});
    await expect(page.locator('.vs-chart-columns')).toBeVisible();
    await page.screenshot({path: `${shots}/window-${width}.png`});
    await page.locator('.vs-chart-columns').screenshot({path: `${shots}/hero-${width}.png`});
    await page
      .getByRole('group', {name: /of each reading over the run/})
      .screenshot({path: `${shots}/chart-${width}.png`});
    await page
      .getByRole('listbox', {name: 'Ideas'})
      .locator('xpath=ancestor::*[contains(@class,"vs-pane")][1]')
      .screenshot({path: `${shots}/ideas-${width}.png`});
  }
});
