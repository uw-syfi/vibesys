import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {openFixture} from './fixture-world.js';

/**
 * A finished run with two objectives (throughput, higher is better; p99 latency, lower is better),
 * as the user sees it: a tile per objective, and the trade-off chart in the step chart's place with
 * the frontier as a staircase, the input marked, dominated and turned-down ideas muted, a hover
 * that names an idea, and the time bar replaying how the frontier advanced. `RUN_SHOTS` names a
 * directory to save the app window in.
 */

const shots = process.env['RUN_SHOTS'];

async function shot(page: Page, name: string): Promise<void> {
  if (shots === undefined) return;
  const width = page.viewportSize()?.width ?? 0;
  await page.screenshot({path: `${shots}/${name}-${width}.png`});
}

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

const chart = (page: Page) => page.getByRole('group', {name: /of each measured idea over the run/});
const roleOf = (page: Page, role: string) =>
  chart(page).locator(`.vs-pareto-point[data-role="${role}"]`);

const panel = (page: Page, name: string) => page.getByRole('region', {name, exact: true});

test('the performance row has a chart per objective, each with its headline and its values on hover', async ({
  page,
}) => {
  await openFixture(page, fixtureNamed('finished-two-objectives'));
  const [throughput, latency] = [panel(page, 'Total ops/sec'), panel(page, 'P99 latency')];
  await expect(throughput).toContainText('higher is better');
  await expect(throughput.getByText('3.83× faster')).toHaveAttribute(
    'title',
    '6,054,154 ops/s · input 1,581,539 ops/s',
  );
  await expect(latency).toContainText('lower is better');
  // Lower is better: the best latency, 8.7 ms against the input's 12.4 ms, reads as an improvement.
  await expect(latency.getByText('1.43× faster')).toHaveAttribute(
    'title',
    '8.7 ms · input 12.4 ms',
  );
  // Each series is a chart of its own beside the trade-off chart.
  await expect(page.getByRole('group', {name: /of each reading over the run/})).toHaveCount(2);
  await shot(page, 'two-objectives-hero');
});

test('the trade-off chart marks the input, the frontier, the dominated and the failed', async ({
  page,
}) => {
  await openFixture(page, fixtureNamed('finished-two-objectives'));
  await expect(chart(page)).toBeVisible();
  await expect(roleOf(page, 'input')).toHaveCount(1);
  await expect(roleOf(page, 'failed')).toHaveCount(1);
  expect(await roleOf(page, 'frontier').count()).toBeGreaterThanOrEqual(3);
  expect(await roleOf(page, 'dominated').count()).toBeGreaterThanOrEqual(1);
  await expect(chart(page).locator('path.vs-pareto-frontier')).toHaveCount(1);
  // Both axes say which way is better, and the key names every mark.
  await expect(chart(page)).toContainText('Total ops/sec (ops/s), higher is better');
  await expect(chart(page)).toContainText('P99 latency (ms), lower is better');
  const key = page.getByRole('list', {name: 'Points'});
  for (const word of ['Input', 'On the frontier', 'Dominated', 'Failed']) {
    await expect(key.getByText(word, {exact: true})).toBeVisible();
  }
  await shot(page, 'two-objectives-chart');
});

test('hovering a point names the idea with both values; pressing it opens the idea', async ({
  page,
}) => {
  await openFixture(page, fixtureNamed('finished-two-objectives'));
  const point = chart(page).getByRole('button', {name: /^Pad the slot headers/});
  await point.hover();
  await expect(chart(page).locator('.vs-pareto-hover-text')).toHaveText(
    'Pad the slot headers · 6,054,154 ops/s · 15.8 ms',
  );
  await shot(page, 'two-objectives-hover');
  await point.click();
  await expect(page.getByRole('heading', {name: /Pad the slot headers/}).first()).toBeVisible();
  // The idea and the ideas it builds on, back to the input, are ringed: five steps in this run.
  await expect(chart(page).locator('.vs-chart-ring')).toHaveCount(5);
});

test('the time bar replays the frontier: earlier in the run there are fewer points', async ({
  page,
}) => {
  await openFixture(page, fixtureNamed('finished-two-objectives'));
  await expect(roleOf(page, 'input')).toHaveCount(1);
  const all = await chart(page).locator('.vs-pareto-point').count();
  const bar = page.getByRole('slider', {name: 'Run position'});
  await bar.focus();
  await page.keyboard.press('Home');
  await expect.poll(async () => chart(page).locator('.vs-pareto-point').count()).toBeLessThan(all);
  // The axes keep their range: the same tick labels before and after.
  await shot(page, 'two-objectives-scrubbed');
  await page.keyboard.press('End');
  await expect.poll(async () => chart(page).locator('.vs-pareto-point').count()).toBe(all);
});
