import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {settled} from './checks/settle.js';
import {type Anchor, anchorsOf, RUN_ANCHORS, stabilityViolations} from './checks/stability.js';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyScript} from './journey-world.js';
import {STARTED_AT} from './started-at.js';

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const INSTANCE = '00000000000a';
const RUN = '20261017-120000';
const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: {width: 84, height: 40},
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [
    {
      kind: 'listed',
      host: 'ssh:host-b',
      live: [
        {
          id: INSTANCE,
          projectRoot: PROJECT,
          runId: RUN,
          status: 'running',
          phase: 'running',
          paused: null,
          end: null,
          startedAt: STARTED_AT,
          reason: null,
        },
      ],
      ended: [],
      unreadable: [],
      unverified: [],
    },
  ],
  recent: [
    {
      host: 'ssh:host-b',
      hostLabel: 'host-b',
      project: PROJECT,
      task: 'mpmc',
      runId: RUN,
      instanceId: INSTANCE,
      attachedAt: 3,
    },
  ],
};

async function openRun(page: Page, count: number): Promise<void> {
  await addInitScripts(page, [
    initScript(installFakeBridge, SEED),
    initScript(installFakeProjectRuns, SEED),
    initScript(installFakeMachines, SEED),
    journeyScript(count > 100 ? 'lineage600' : 'lineage10'),
  ]);
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();
}

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

test('ideas stay flat, searchable, sortable and accessible at wide and narrow widths', async ({
  page,
}) => {
  await openRun(page, 10);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  await expect(ideas.locator('.vs-guide, .vs-pickrow-fold')).toHaveCount(0);
  await expect(page.getByRole('radiogroup', {name: 'Ideas layout'})).toHaveCount(0);
  await expect(page.getByRole('combobox', {name: 'Sort ideas'})).toHaveValue('newest');
  await page.getByRole('combobox', {name: 'Sort ideas'}).selectOption('oldest');
  await expect(ideas.getByRole('option').first()).toContainText('Preallocate the arena');
  await page.getByRole('searchbox', {name: 'Search ideas'}).fill('Batch the enqueue');
  await expect(ideas.getByRole('option')).toHaveCount(1);
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await page.setViewportSize({width: 1000, height: 800});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await ideas.getByRole('option').first().click();
  await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
  await expect(page.getByText(/vs base/)).toHaveCount(0);
  await expect(page.getByText(/^from$/)).toHaveCount(0);
});

test('six hundred ideas render a window of rows and remain keyboard reachable', async ({page}) => {
  await openRun(page, 600);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  expect(await ideas.getByRole('option').count()).toBeLessThan(80);
  await ideas.focus();
  await page.keyboard.press('End');
  await expect(ideas.getByRole('option').last()).toBeFocused();
  // Rows are measured as they are drawn, so the end of the list moves until its rows are at rest.
  await settled(page);
  const scrollTop = await ideas.evaluate(list => list.scrollTop);
  await ideas.evaluate(list => list.setAttribute('data-test-persist', 'yes'));
  await page.keyboard.press('Enter');
  await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
  await expect(ideas).toHaveAttribute('data-test-persist', 'yes');
  await settled(page);
  expect(await ideas.evaluate(list => list.scrollTop)).toBe(scrollTop);
});

/** Rows wrap at some window sizes and not at others, so the list's rows have several heights. */
const WINDOWS = [
  {width: 1440, height: 900},
  {width: 1200, height: 800},
  {width: 1100, height: 760},
  {width: 1000, height: 700},
  {width: 1000, height: 900},
] as const;

for (const {width, height} of WINDOWS) {
  test(`opening the last idea leaves the Ideas list where the end key put it at ${width}x${height}`, async ({
    page,
  }) => {
    await page.setViewportSize({width, height});
    await openRun(page, 600);
    const ideas = page.getByRole('listbox', {name: 'Ideas'});
    await expect(ideas.getByRole('option').first()).toBeVisible();
    await ideas.focus();
    await page.keyboard.press('End');
    await expect(ideas.getByRole('option').last()).toBeFocused();
    await settled(page);
    const scrollTop = await ideas.evaluate(list => list.scrollTop);
    await page.keyboard.press('Enter');
    await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
    await settled(page);
    expect(await ideas.evaluate(list => list.scrollTop)).toBe(scrollTop);
  });
}

test('opening an idea preserves the Ideas list and controls at wide and narrow widths', async ({
  page,
}) => {
  await openRun(page, 10);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  const controls = page.locator('.vs-pane', {has: ideas});
  const detail = page.locator('.vs-pane[data-width="fill"]');
  await expect(
    ideas.locator('.vs-chip[data-tone="ok"]', {hasText: 'Passed'}).first(),
  ).toBeVisible();
  const geometry = () =>
    ideas.evaluate(list => {
      const pane = list.closest('.vs-pane');
      const row = list.querySelector('.vs-pickrow');
      const figure = row?.querySelector('.vs-pickrow-figure');
      const bounds = (element: Element | null | undefined) => {
        const rect = element?.getBoundingClientRect();
        return rect === undefined ? null : [rect.x, rect.y, rect.width, rect.height];
      };
      return {
        pane: bounds(pane),
        list: bounds(list),
        row: bounds(row),
        figure: bounds(figure),
        scrollTop: list.scrollTop,
        text: row?.querySelector('.vs-pickrow-text')?.textContent,
      };
    });
  for (const width of [1440, 1000]) {
    await page.setViewportSize({width, height: 900});
    // The layout is measured only once it has stopped moving after the resize.
    await settled(page);
    await expect(controls.locator('.vs-dotlegend-item')).toHaveCount(3);
    await expect(page.getByRole('searchbox', {name: 'Search ideas'})).toBeVisible();
    await expect(page.getByRole('combobox', {name: 'Sort ideas'})).toBeVisible();
    await ideas.evaluate(list => list.setAttribute('data-test-persist', 'yes'));
    const before = await geometry();
    if (process.env['RUN_SHOTS'] !== undefined) {
      await page.screenshot({path: `${process.env['RUN_SHOTS']}/ideas-closed-${width}.png`});
    }
    await ideas.getByRole('option').first().click();
    await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
    await expect(controls.locator('.vs-dotlegend-item')).toHaveCount(3);
    await expect(ideas).toHaveAttribute('data-test-persist', 'yes');
    await expect(page.getByRole('searchbox', {name: 'Search ideas'})).toBeVisible();
    await expect(page.getByRole('combobox', {name: 'Sort ideas'})).toBeVisible();
    if (process.env['RUN_SHOTS'] !== undefined) {
      await page.screenshot({path: `${process.env['RUN_SHOTS']}/ideas-open-${width}.png`});
    }
    expect(await geometry()).toEqual(before);
    await detail.getByRole('radio', {name: 'Logs'}).check({force: true});
    await expect(detail.getByRole('radio', {name: 'Logs'})).toBeChecked();
    await expect(ideas).toHaveAttribute('data-test-persist', 'yes');
    await expect(controls.locator('.vs-dotlegend-item')).toHaveCount(3);
    expect(await geometry()).toEqual(before);
    await detail.getByRole('radio', {name: 'Steps'}).check({force: true});
    await expect(detail.getByRole('radio', {name: 'Steps'})).toBeChecked();
    expect(await geometry()).toEqual(before);
    await page.getByRole('button', {name: 'Close idea'}).click();
    await expect(ideas).toHaveAttribute('data-test-persist', 'yes');
    expect(await geometry()).toEqual(before);
  }
});

/** What stays through a change of idea: the list and the chart (the steps are another idea's). */
const PANES: readonly Anchor[] = [
  ...RUN_ANCHORS,
  {name: 'ideas list', selector: '[role="listbox"][aria-label="Ideas"]'},
];

test('choosing another idea keeps the list, the detail and the chart where they are', async ({
  page,
}) => {
  await openRun(page, 10);
  const options = page.getByRole('listbox', {name: 'Ideas'}).getByRole('option');
  await settled(page);
  await options.nth(0).click();
  await expect(page.getByRole('listbox', {name: /steps$/})).toBeVisible();
  await settled(page);
  const before = await anchorsOf(page, PANES);
  const steps = page.getByRole('listbox', {name: /steps$/});
  // The steps list is as tall as the idea's steps, so only where it starts and how wide it is hold.
  const placed = async () => {
    const box = await steps.boundingBox();
    return box === null ? null : {x: box.x, y: box.y, width: box.width};
  };
  const stepsBefore = await placed();
  for (const index of [1, 2, 0]) {
    await options.nth(index).hover();
    await settled(page);
    await options.nth(index).click();
    await expect(options.nth(index)).toHaveAttribute('aria-selected', 'true');
    await settled(page);
    expect(await stabilityViolations(page, `idea ${index}`, before, PANES)).toEqual([]);
    expect(await placed()).toEqual(stepsBefore);
  }
});

test('regression: a wide window gives the idea titles room, the same before and after one opens', async ({
  page,
}) => {
  await openRun(page, 10);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  const titleWidth = () =>
    ideas
      .locator('.vs-pickrow-text')
      .first()
      .evaluate(text => text.getBoundingClientRect().width);
  const paneWidth = () =>
    page.locator('.vs-pane', {has: ideas}).evaluate(pane => pane.getBoundingClientRect().width);
  await settled(page);
  const closed = {pane: await paneWidth(), title: await titleWidth()};
  // 340 px of pane gave a title about 20 characters of the monospace face.
  expect(closed.pane).toBeGreaterThanOrEqual(480);
  expect(closed.title).toBeGreaterThanOrEqual(250);
  await ideas.getByRole('option').first().click();
  await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
  await settled(page);
  expect({pane: await paneWidth(), title: await titleWidth()}).toEqual(closed);
  await page.setViewportSize({width: 1000, height: 800});
  await settled(page);
  expect(await paneWidth()).toBeGreaterThanOrEqual(340);
});

test('regression: no idea title is cut short with an ellipsis, closed or open, at any width', async ({
  page,
}) => {
  await openRun(page, 10);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  const cut = () =>
    ideas
      .locator('.vs-pickrow-text > .vs-text:first-child')
      .evaluateAll(titles =>
        titles
          .filter(title => title.scrollWidth > title.clientWidth)
          .map(title => title.textContent),
      );
  for (const width of [1440, 1000]) {
    await page.setViewportSize({width, height: 900});
    await settled(page);
    expect(await cut(), `closed at ${width}`).toEqual([]);
    await ideas.getByRole('option').first().click();
    await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
    await settled(page);
    expect(await cut(), `open at ${width}`).toEqual([]);
    await page.getByRole('button', {name: 'Close idea'}).click();
  }
});
