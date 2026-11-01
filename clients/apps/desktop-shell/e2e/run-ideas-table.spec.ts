import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import fc from 'fast-check';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeProjectRuns,
  MAC_CHROME,
} from '../src/testing/fake-bridge.js';
import {STARTED_AT} from '../src/testing/started-at.js';
import {scrolledToEnd, settled} from './checks/settle.js';
import {type Anchor, anchorsOf, RUN_ANCHORS, stabilityViolations} from './checks/stability.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {journeyScript} from './journey-world.js';

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const INSTANCE = '00000000000a';
const RUN = '20261017-120000';
const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: MAC_CHROME,
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
    initScript(installFakeChrome, SEED),
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
  // The list opens sorted by its first objective, whose header is a label, not a control.
  await expect(page.locator('.vs-columnlist-headcell[data-marked]')).toHaveCount(1);
  await expect(page.locator('.vs-columnlist-headcell[data-marked] button')).toHaveCount(0);
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

/**
 * The list is as wide as the window leaves it, so opening an idea beside it (or closing it) narrows
 * it and rewraps its rows, and the rows below the reader are measured after the list has scrolled to
 * the one that opened. What holds is that the opened row is picked and drawn within a screenful of
 * the list's visible part (it is never lost), and that the list keeps what the reader had chosen.
 */
async function expectInView(page: Page, label: string): Promise<void> {
  const list = page.getByRole('listbox', {name: 'Ideas'});
  const [box, row] = await Promise.all([
    list.boundingBox(),
    list.getByRole('option', {selected: true}).boundingBox(),
  ]);
  expect(box, `${label}: the list has a box`).not.toBeNull();
  expect(row, `${label}: the picked row is drawn`).not.toBeNull();
  const room = box?.height ?? 0;
  expect((row?.y ?? 0) + (row?.height ?? 0), label).toBeLessThanOrEqual(
    (box?.y ?? 0) + (box?.height ?? 0) + room,
  );
  expect(row?.y ?? 0, label).toBeGreaterThanOrEqual((box?.y ?? 0) - room);
}

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
  await ideas.evaluate(list => list.setAttribute('data-test-persist', 'yes'));
  await page.keyboard.press('Enter');
  await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
  await expect(ideas).toHaveAttribute('data-test-persist', 'yes');
  await settled(page);
  await expectInView(page, 'the opened row after opening it');
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
    await page.keyboard.press('Enter');
    await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
    await settled(page);
    await expectInView(page, `the opened row after opening it at ${width}x${height}`);
  });
}

/**
 * What a reader does to a list: a wheel turn, any key that scrolls, or a drag or touch (which move
 * the scroll without a key or wheel event, so the one move a script can make stands for all of
 * them: `scroll` sets the position and leaves its scroll event pending for the same task).
 */
type Reader =
  | {readonly kind: 'wheel'; readonly delta: number}
  | {readonly kind: 'key'; readonly key: string}
  | {readonly kind: 'scroll'; readonly fraction: number};

const READER: fc.Arbitrary<Reader> = fc.oneof(
  fc.record({kind: fc.constant('wheel' as const), delta: fc.integer({min: -2400, max: 2400})}),
  fc.record({
    kind: fc.constant('key' as const),
    key: fc.constantFrom('PageUp', 'PageDown', 'Space', 'Home', 'End', 'ArrowUp', 'ArrowDown'),
  }),
  fc.record({
    kind: fc.constant('scroll' as const),
    fraction: fc.double({min: 0, max: 1, noNaN: true}),
  }),
);

/**
 * Opens the idea of the row most in view, or closes the open one: a commit that lays the list out
 * again. The scroll it began with is read in the same task, so a move of it still waiting as an
 * event counts as where the reader left the list.
 */
async function commitFrom(page: Page, move: number | null): Promise<number> {
  return page.evaluate(fraction => {
    const list = document.querySelector<HTMLElement>('[role="listbox"][aria-label="Ideas"]');
    if (list === null) throw new Error('the Ideas list is not on screen');
    if (fraction !== null) {
      list.scrollTop = fraction * (list.scrollHeight - list.clientHeight);
    }
    const at = list.scrollTop;
    const close = document.querySelector<HTMLElement>('button[aria-label="Close idea"]');
    if (close !== null) {
      close.click();
      return at;
    }
    const box = list.getBoundingClientRect();
    const shown = (row: HTMLElement): number => {
      const rect = row.getBoundingClientRect();
      return Math.min(rect.bottom, box.bottom) - Math.max(rect.top, box.top);
    };
    const rows = [...list.querySelectorAll<HTMLElement>('[role="option"]')];
    const row = rows.sort((a, b) => shown(b) - shown(a))[0];
    if (row === undefined) throw new Error('no row is drawn');
    row.click();
    return at;
  }, move);
}

test('no scroll of the reader is undone by a commit, whatever the reader did before it', async ({
  page,
}) => {
  await page.setViewportSize({width: 1000, height: 700});
  await openRun(page, 600);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  await settled(page);
  const [steps] = fc.sample(fc.array(READER, {minLength: 24, maxLength: 24}), {
    numRuns: 1,
    seed: 20261023,
  });
  for (const step of steps ?? []) {
    let move: number | null = null;
    if (step.kind === 'wheel') {
      const box = await ideas.boundingBox();
      if (box === null) throw new Error('the Ideas list has no box');
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.wheel(0, step.delta);
      await settled(page);
    } else if (step.kind === 'key') {
      await page.keyboard.press(step.key);
      await settled(page);
    } else {
      move = step.fraction;
    }
    const at = await commitFrom(page, move);
    await settled(page);
    // A commit that opens or closes an idea narrows or widens the list and rewraps its rows, which
    // moves the scroll by a few rows' height at most; a reader's scroll undone is far more.
    const now = await ideas.evaluate(list => list.scrollTop);
    expect(Math.abs(now - at), JSON.stringify(step)).toBeLessThanOrEqual(200);
  }
});

test('opening an idea preserves the Ideas list and controls at wide and narrow widths', async ({
  page,
}) => {
  await openRun(page, 10);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  const controls = page.locator('.vs-pane', {has: ideas});
  const detail = page.locator('.vs-pane[data-width="fill"]');
  await expect(ideas.getByRole('option').first()).toBeVisible();
  const chips = await controls.locator('.vs-dotlegend-item').count();
  expect(chips).toBeGreaterThan(1);
  const geometry = () =>
    ideas.evaluate(list => {
      const pane = list.closest('.vs-pane');
      const row = list.querySelector('.vs-columnlist-row');
      const bounds = (element: Element | null | undefined) => {
        const rect = element?.getBoundingClientRect();
        return rect === undefined ? null : [rect.x, rect.y, rect.width, rect.height];
      };
      return {
        // Where the pane starts, not how wide it is: an idea opened beside it takes its width, and
        // the controls above the rows wrap to the room they have.
        pane: bounds(pane)?.slice(1, 2),
        scrollTop: list.scrollTop,
        text: row?.querySelector('.vs-columnlist-cell')?.textContent,
      };
    });
  for (const width of [1440, 1000]) {
    // Tall enough for the charts and the whole list: pressing a row scrolls nothing.
    await page.setViewportSize({width, height: 1400});
    // The layout is measured only once it has stopped moving after the resize.
    await settled(page);
    await expect(controls.locator('.vs-dotlegend-item')).toHaveCount(chips);
    await expect(page.getByRole('searchbox', {name: 'Search ideas'})).toBeVisible();
    await ideas.evaluate(list => list.setAttribute('data-test-persist', 'yes'));
    const before = await geometry();
    if (process.env['RUN_SHOTS'] !== undefined) {
      await page.screenshot({path: `${process.env['RUN_SHOTS']}/ideas-closed-${width}.png`});
    }
    await ideas.getByRole('option').first().click();
    await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
    await expect(controls.locator('.vs-dotlegend-item')).toHaveCount(chips);
    await expect(ideas).toHaveAttribute('data-test-persist', 'yes');
    await expect(page.getByRole('searchbox', {name: 'Search ideas'})).toBeVisible();
    if (process.env['RUN_SHOTS'] !== undefined) {
      await page.screenshot({path: `${process.env['RUN_SHOTS']}/ideas-open-${width}.png`});
    }
    expect(await geometry()).toEqual(before);
    await detail.getByRole('radio', {name: 'Logs'}).check({force: true});
    await expect(detail.getByRole('radio', {name: 'Logs'})).toBeChecked();
    await expect(ideas).toHaveAttribute('data-test-persist', 'yes');
    await expect(controls.locator('.vs-dotlegend-item')).toHaveCount(chips);
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
  await scrolledToEnd(page);
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

test('regression: a wide window gives the idea titles room, before and after one opens', async ({
  page,
}) => {
  await openRun(page, 10);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  const titleWidth = () =>
    ideas
      .locator('.vs-columnlist-cell')
      .first()
      .evaluate(text => text.getBoundingClientRect().width);
  const paneWidth = () =>
    page.locator('.vs-pane', {has: ideas}).evaluate(pane => pane.getBoundingClientRect().width);
  await settled(page);
  const closed = {pane: await paneWidth(), title: await titleWidth()};
  // 340 px of pane gave a title about 20 characters of the monospace face.
  expect(closed.pane).toBeGreaterThanOrEqual(480);
  expect(closed.title).toBeGreaterThanOrEqual(230);
  await ideas.getByRole('option').first().click();
  await expect(page.getByRole('button', {name: 'Close idea'})).toBeVisible();
  await settled(page);
  // The idea opens beside the list, which gives it room; its titles keep theirs.
  const open = {pane: await paneWidth(), title: await titleWidth()};
  expect(open.pane).toBeLessThanOrEqual(closed.pane);
  expect(open.title).toBeGreaterThanOrEqual(230);
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
      .locator('.vs-columnlist-cell:first-child')
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
