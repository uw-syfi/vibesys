import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * Layout and command behaviour of the Run screen that the generic screen checks
 * (`checks/in-page.ts`) cannot decide on their own: which region scrolls, which column a header
 * sits over, how the panes share the row, what the time bar labels, and what a command that fails
 * shows. Each reads the fixtures the screen checks walk.
 */

async function open(
  page: Page,
  name: string,
  width: number,
  refuseActions: readonly string[] = [],
): Promise<void> {
  await page.setViewportSize({width, height: 900});
  const fixture = fixtureNamed(name);
  await openFixture(page, fixture, {refuseActions});
  await playLive(page, fixture);
  await expect(page.getByRole('slider', {name: 'Run position'})).toBeVisible();
  await settled(page);
}

const tickLabels = (page: Page): Promise<string[]> =>
  page
    .locator('.vs-scrubber-tick')
    .evaluateAll(ticks => ticks.map(tick => (tick.textContent ?? '').trim()));

for (const width of [660, 1000, 1440]) {
  test(`at ${width} px a two-call run labels its calls "Call 1" and "Call 2"`, async ({page}) => {
    await open(page, 'finished-run', width);
    expect(await tickLabels(page)).toEqual(['Call 1', 'Call 2']);
  });

  test(`at ${width} px a fifteen-call run labels every call, by number, in order`, async ({
    page,
  }) => {
    await open(page, 'weeks-600-ideas', width);
    expect(await tickLabels(page)).toEqual(
      Array.from({length: 15}, (_, index) => String(index + 1)),
    );
  });
}

test('scrolling the ideas list moves only the list: the tabs, chips and search stay where they are', async ({
  page,
}) => {
  await open(page, 'weeks-600-ideas', 1440);
  const search = page.getByRole('searchbox', {name: 'Search ideas'});
  const header = page.locator('.vs-columnlist-head').first();
  const before = [await search.boundingBox(), await header.boundingBox()];
  await page.getByRole('listbox', {name: 'Ideas'}).evaluate(list => {
    list.scrollTop = 600;
  });
  await settled(page);
  expect([await search.boundingBox(), await header.boundingBox()]).toEqual(before);
  const body = page.locator('.vs-pane-body[data-fixed]').first();
  expect(await body.evaluate(element => element.scrollTop)).toBe(0);
  // Focusing a row never scrolls the pane under its tabs either.
  await page.getByRole('listbox', {name: 'Ideas'}).getByRole('option').nth(3).focus();
  expect(await body.evaluate(element => element.scrollTop)).toBe(0);
  expect(await page.getByRole('radiogroup', {name: 'Show'}).first().isVisible()).toBe(true);
});

test('the agents header columns sit over their values, right edge to right edge', async ({
  page,
}) => {
  await open(page, 'queue-rs-live', 1440);
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
  const agents = page.getByRole('listbox', {name: 'Agents'});
  await expect(agents.getByRole('option').first()).toBeVisible();
  const edges = await page.evaluate(() => {
    const list = document.querySelector('.vs-picklist:has([aria-label="Agents"])');
    const right = (selector: string): number[] =>
      [...(list?.querySelectorAll(selector) ?? [])].map(each => each.getBoundingClientRect().right);
    return {
      took: right('.vs-listheader-col[data-fixed="time"]'),
      values: right('.vs-pickrow-trail[data-short]'),
      turns: right('.vs-listheader-col[data-fixed="figure"]'),
      counts: right('.vs-pickrow-figure'),
    };
  });
  expect(edges.took).toHaveLength(1);
  for (const value of edges.values)
    expect(Math.abs(value - (edges.took[0] ?? 0))).toBeLessThan(1.5);
  for (const count of edges.counts)
    expect(Math.abs(count - (edges.turns[0] ?? 0))).toBeLessThan(1.5);
});

test('the Ideas list takes the whole row when nothing sits beside it', async ({page}) => {
  await open(page, 'finished-run', 1000);
  const widths = await page.evaluate(() => ({
    pane: document.querySelector('.vs-panes > .vs-pane')?.getBoundingClientRect().width ?? 0,
    panes: document.querySelector('.vs-panes')?.getBoundingClientRect().width ?? 0,
  }));
  expect(Math.abs(widths.pane - widths.panes)).toBeLessThan(1);
});

test('the Now pane takes what the Ideas list leaves, to the edge of the screen', async ({page}) => {
  await open(page, 'queue-rs-live', 1440);
  const edges = await page.evaluate(() => ({
    now:
      document.querySelector('.vs-panes > .vs-pane:last-child')?.getBoundingClientRect().right ?? 0,
    panes: document.querySelector('.vs-panes')?.getBoundingClientRect().right ?? 0,
    header:
      document.querySelector('button[aria-label="More actions"]')?.getBoundingClientRect().right ??
      0,
  }));
  expect(Math.abs(edges.now - edges.panes)).toBeLessThan(1);
  expect(edges.now).toBeGreaterThan(edges.header - 12);
});

test('at 1000 px the performance chart is readable and inside the screen', async ({page}) => {
  await open(page, 'finished-run', 1000);
  const boxes = await page.evaluate(() => {
    const chart = document.querySelector('svg.vs-chart')?.getBoundingClientRect();
    return {width: chart?.width ?? 0, right: chart?.right ?? 0, screen: window.innerWidth};
  });
  expect(boxes.width).toBeGreaterThanOrEqual(280);
  expect(boxes.right).toBeLessThanOrEqual(boxes.screen);
});

test('an idea is named in the monospace face in its pane title, its Steps title and the breadcrumb', async ({
  page,
}) => {
  await open(page, 'finished-run', 1440);
  await page.getByRole('listbox', {name: 'Ideas'}).getByRole('option').first().click();
  await expect(page.getByRole('listbox', {name: /steps$/})).toBeVisible();
  const mono = await page.evaluate(() => {
    const family = (element: Element | null): string =>
      element === null ? '' : getComputedStyle(element).fontFamily;
    const row = document.querySelector('[aria-label="Ideas"] [role="option"] .vs-text');
    return {
      row: family(row),
      title: family(document.querySelector('.vs-pane-title[data-mono]')),
      titles: document.querySelectorAll('.vs-pane-title[data-mono]').length,
    };
  });
  expect(mono.title).toBe(mono.row);
  expect(mono.titles).toBeGreaterThan(0);
  await page
    .getByRole('listbox', {name: /steps$/})
    .getByRole('option')
    .first()
    .click();
  const crumb = await page.evaluate(() => ({
    idea: getComputedStyle(
      [...document.querySelectorAll('.vs-crumb')].filter(each =>
        each.hasAttribute('data-mono'),
      )[0] ?? document.body,
    ).fontFamily,
    row: getComputedStyle(document.body).fontFamily,
    marked: document.querySelectorAll('.vs-crumb[data-mono]').length,
    steps: getComputedStyle(document.querySelector('.vs-pane-title[data-mono]') ?? document.body)
      .fontFamily,
  }));
  expect(crumb.marked).toBe(1);
  expect(crumb.idea).toBe(crumb.steps);
  expect(crumb.idea).not.toBe(crumb.row);
});

test('a Pause the run does not take is shown as not confirmed, never thrown, and Pause now keeps its button', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await open(page, 'queue-rs-live', 1440, ['pause']);
  await page.getByRole('button', {name: 'Pause', exact: true}).click();
  await expect(page.getByText('Pause · Not confirmed', {exact: true})).toBeVisible();
  expect(errors).toEqual([]);
  const stop = page.getByRole('button', {name: 'Pause now', exact: true});
  await expect(stop).toBeVisible();
  await expect(stop).not.toHaveAttribute('aria-disabled', 'true');
  await stop.click();
  await expect(page.getByText('Pause now · Not confirmed', {exact: true})).toBeVisible();
  expect(errors).toEqual([]);
  await expect(stop).not.toHaveAttribute('aria-disabled', 'true');
});

test('the face switcher keeps its place and width from the Ideas face to the others and back', async ({
  page,
}) => {
  await open(page, 'queue-rs-live', 1440);
  const switcher = page
    .getByRole('radiogroup', {name: 'Show'})
    .filter({has: page.getByText('Timeline')})
    .first();
  const place = async (): Promise<unknown> => {
    const box = await switcher.boundingBox();
    return box && [box.x, box.y, box.width, box.height].map(value => Math.round(value));
  };
  const ideas = await place();
  for (const face of ['Timeline', 'Agents', 'Logs', 'Ideas']) {
    await page
      .getByRole('radiogroup', {name: 'Show'})
      .first()
      .getByText(face, {exact: true})
      .click();
    await settled(page);
    expect({face, at: await place()}).toEqual({face, at: ideas});
  }
});
