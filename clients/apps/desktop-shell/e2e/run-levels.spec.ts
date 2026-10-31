import AxeBuilder from '@axe-core/playwright';
import {expect, type Locator, type Page, test} from '@playwright/test';
import type {RunFixture} from '@vibesys/run-fixtures';
import {
  type FakeBridgeSeed,
  type FakeWindows,
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeProjectRuns,
  installFakeWindows,
  MAC_CHROME,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {appendJourney, journeyFixture, journeyScript, transitionJourney} from './journey-world.js';
import {nativeFixtureScript} from './native-fixture.js';
import {STARTED_AT} from './started-at.js';

/**
 * The Run screen's levels against the Fake bridge and native gateway records, including
 * a dynamic run and W5's transcript: the run level (the hero, the ideas, Now), an idea, a
 * step and a step's item, reached by click, walked back by Escape, the breadcrumb and the
 * browser's Back, with no page scroll at any level. Checked in the Ink theme at 1440×900;
 * `LEVEL_SHOTS` names a directory to save the screens in (the app's main region only).
 */

interface FakeHostWindow extends Window {
  fakeHost: {listing(listing: unknown): void; emit(events: readonly unknown[]): void};
}

const PROJECT = '/home/me/vibesys-projects/queue-rs';
const RUN_ID = '20261015-163047';
const INSTANCE = '00000000000a';

function server(phase: string, end: unknown = null): Record<string, unknown> {
  return {
    id: INSTANCE,
    projectRoot: PROJECT,
    runId: RUN_ID,
    status: phase,
    phase,
    paused: null,
    end,
    startedAt: STARTED_AT,
    reason: null,
  };
}

function listed(live: unknown[], ended: unknown[] = []): Record<string, unknown> {
  return {kind: 'listed', host: 'ssh:host-b', live, ended, unreadable: [], unverified: []};
}

const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: MAC_CHROME,
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [listed([server('running')])],
  recent: [
    {
      host: 'ssh:host-b',
      hostLabel: 'host-b',
      project: PROJECT,
      task: 'mpmc',
      runId: RUN_ID,
      instanceId: INSTANCE,
      attachedAt: 3,
    },
  ],
};

const SERVER = journeyFixture('trace');

/** The address of a run at its own level: it names the run, and no zoom. */
const RUN_LEVEL = /#run=[^&]+$/;

const shots = process.env['LEVEL_SHOTS'];

async function check(page: Page, name: string): Promise<void> {
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  // The page is fixed: whatever the level, it never scrolls; its panes do.
  expect(
    await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight),
  ).toBe(true);
  if (shots !== undefined) await page.getByRole('main').screenshot({path: `${shots}/${name}.png`});
}

async function openRun(page: Page, live = false, server: RunFixture = SERVER): Promise<void> {
  await addInitScripts(page, [
    initScript(installFakeChrome, SEED),
    initScript(installFakeBridge, SEED),
    initScript(installFakeProjectRuns, SEED),
    initScript(installFakeMachines, SEED),
    live
      ? journeyScript('live')
      : server === SERVER
        ? journeyScript('trace')
        : nativeFixtureScript(server),
    ...(live ? [initScript(installFakeWindows, undefined)] : []),
  ]);
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .click();
}

/** The Ideas pane is always a flat list. */
async function showList(page: Page): Promise<void> {
  await expect(page.getByRole('listbox', {name: 'Ideas'})).toBeVisible();
}

/** Presses Tab until the focus is inside `target`; at most `limit` presses (guards a hang). */
async function tabInto(page: Page, target: Locator, limit = 60): Promise<void> {
  for (let press = 0; press < limit; press += 1) {
    if (await target.evaluate(element => element.contains(document.activeElement))) return;
    await page.keyboard.press('Tab');
  }
  throw new Error(`Tab never reached ${String(target)}`);
}

test.use({colorScheme: 'dark', viewport: {width: 1440, height: 900}});

test('every level opens from the keyboard alone, and Escape walks back', async ({page}) => {
  await openRun(page);
  await showList(page);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option').first()).toBeVisible();
  await tabInto(page, ideas);
  // Down to the third row, then Enter opens it.
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', {name: 'Combine W1 ring with W2 padding'})).toBeVisible();

  const steps = page.getByRole('listbox', {name: /steps$/});
  await tabInto(page, steps);
  await page.keyboard.press('Home');
  const implementer = steps.getByRole('option', {name: /builder/i}).first();
  for (let press = 0; press < 20; press += 1) {
    if (await implementer.evaluate(element => element === document.activeElement)) break;
    await page.keyboard.press('ArrowDown');
  }
  await page.keyboard.press('Enter');
  const turn = page.getByRole('region', {name: 'Agent turn'});
  await expect(turn).toBeVisible();

  // Tab reaches the reasoning line, then the tool group and its calls; Enter on the call opens it.
  await tabInto(page, turn);
  const bench = turn.getByRole('button', {name: /cargo bench --bench queue/}).last();
  for (let press = 0; press < 10; press += 1) {
    if (await bench.evaluate(element => element === document.activeElement)) break;
    await page.keyboard.press('Tab');
  }
  await page.keyboard.press('Enter');
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toBeVisible();

  for (const _level of ['step', 'idea', 'run']) await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox', {name: 'Running now'})).toBeVisible();
  await expect(page.getByRole('heading', {name: 'Combine W1 ring with W2 padding'})).toHaveCount(0);
});

test('the run level shows the best ratio, the ideas running first, and what runs now', async ({
  page,
}) => {
  await openRun(page);
  await showList(page);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  // Running first, then the rest newest first, the failed among them.
  await expect(ideas.getByRole('option')).toHaveText([
    /Per-core queues with stealing/,
    /Wait-free consumer path/,
    /Combine W1 ring with W2 padding/,
    /Larger batch size/,
    /Spin then park idle consumers/,
    /Cache-line padding and batched enqueue/,
    /Lock-free ring with per-slot sequence numbers/,
  ]);
  await expect(page.getByText('1.18×').first()).toBeVisible();
  const now = page.getByRole('listbox', {name: 'Running now'});
  await expect(now.getByRole('option', {name: /Combine W1 ring/}).first()).toBeVisible();
  await check(page, 'ink-run');

  // The failed are listed; their chip narrows the list to them and a second press brings the rest
  // back, and the search narrows the list.
  await page.locator('button.vs-dotlegend-item', {hasText: '2 failed'}).click();
  await expect(ideas.getByRole('option', {name: /Spin then park/})).toBeVisible();
  await expect(ideas.getByRole('option', {name: /Combine W1 ring/})).toHaveCount(0);
  await page.locator('button.vs-dotlegend-item', {hasText: '2 failed'}).click();
  await expect(ideas.getByRole('option', {name: /Combine W1 ring/})).toBeVisible();
  await page.getByLabel('Search ideas').fill('spin');
  await expect(ideas.getByRole('option')).toHaveText([/Spin then park/]);

  // The mode switch keeps the filter in the page state.
  const show = page.getByRole('radiogroup', {name: 'Show'});
  await show.getByText('Agents').click();
  await show.getByText('Ideas').click();
  await expect(page.getByLabel('Search ideas')).toHaveValue('spin');
});

test('every level opens by click and walks back by Escape, the crumbs and Back', async ({page}) => {
  await openRun(page);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});

  // Level 1: the idea beside the ideas, with what it built on.
  await ideas.getByRole('option', {name: /Combine W1 ring with W2 padding/}).click();
  await expect(page.getByRole('heading', {name: 'Combine W1 ring with W2 padding'})).toBeVisible();
  await expect(page.getByText(/^from$/)).toHaveCount(0);
  await check(page, 'ink-idea');

  // Level 2: a step beside the idea's steps.
  const steps = page.getByRole('listbox', {name: /steps$/});
  await steps
    .getByRole('option', {name: /builder/i})
    .first()
    .click();
  // The turn reads as narrative: thinking (closed to a one-line preview) and text first, then the
  // tool calls as rows; a short run of calls starts open, and the reader can close it.
  const turn = page.getByRole('region', {name: 'Agent turn'});
  await expect(turn.getByText('The head and tail counters share a cache line.')).toBeVisible();
  await expect(turn.getByText('I will pad each counter to its own line')).toBeVisible();
  const group = turn.getByRole('button', {name: /1 tool/});
  await expect(group).toHaveAttribute('aria-expanded', 'true');
  await group.click();
  await expect(group).toHaveAttribute('aria-expanded', 'false');
  await group.click();
  await expect(group).toHaveAttribute('aria-expanded', 'true');
  await check(page, 'ink-step');

  // Level 3: the item in full, beside its step, whose group stays open with the call marked.
  await turn
    .getByRole('button', {name: /cargo bench --bench queue/})
    .last()
    .click();
  await expect(turn.locator('[aria-current="true"]')).toBeVisible();
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toBeVisible();
  await check(page, 'ink-item');

  // Escape goes up a level, Back retraces the zooms, the breadcrumb jumps to the run.
  await page.keyboard.press('Escape');
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toHaveCount(0);
  await expect(turn).toBeVisible();
  // Escape is a zoom of its own: Back returns to the item, Forward to the step again.
  await page.goBack();
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toBeVisible();
  await page.goForward();
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toHaveCount(0);
  await expect(turn).toBeVisible();
  // Each crumb is a zoom the address follows, so Back returns to the level it left.
  const crumbs = page.getByRole('navigation', {name: 'Zoom'});
  const stepHash = await page.evaluate(() => window.location.hash);
  await crumbs.getByRole('button', {name: 'Combine W1 ring with W2 padding'}).click();
  await expect(turn).toHaveCount(0);
  await expect(page).toHaveURL(/&idea=[^&]+$/);
  await page.goBack();
  await expect(turn).toBeVisible();
  expect(await page.evaluate(() => window.location.hash)).toBe(stepHash);
  await crumbs.getByRole('button', {name: 'Run', exact: true}).click();
  await expect(page.getByRole('listbox', {name: 'Running now'})).toBeVisible();
  await expect(page).toHaveURL(RUN_LEVEL);
  await page.goBack();
  await expect(turn).toBeVisible();
  await page.goForward();
  await expect(page.getByRole('listbox', {name: 'Running now'})).toBeVisible();

  // Now jumps straight to the live step; Escape walks up to the run.
  await page
    .getByRole('listbox', {name: 'Running now'})
    .getByRole('option', {name: /Combine W1 ring/})
    .first()
    .click();
  await expect(page.getByRole('heading', {name: 'correctness + benchmark'})).toBeVisible();
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox', {name: 'Running now'})).toBeVisible();
});

/** The performance chart of the run, which every level keeps on screen. */
function perfChart(page: Page): Locator {
  return page.getByRole('group', {name: /of each reading over the run/});
}

/** Where the performance row is and what its axes read: all of it must be the same at every level. */
async function perfRow(page: Page) {
  return perfChart(page).evaluate(chart => {
    const box = (element: Element) => {
      const {x, y, width, height} = element.getBoundingClientRect();
      return {x, y, width, height};
    };
    const row = chart.closest('.vs-chart-columns');
    return {
      chart: box(chart),
      row: row === null ? null : box(row),
      axes: [...chart.querySelectorAll('text.vs-chart-label')].map(label => label.textContent),
      probe: chart.getAttribute('data-probe'),
    };
  });
}

/** The readings ringed in the text color (the open idea's) and in the accent (its lineage's steps). */
async function ringed(page: Page) {
  return perfChart(page).evaluate(chart => {
    const named = (ring: string) =>
      [...chart.querySelectorAll('.vs-chart-pick')]
        .filter(point => point.querySelector(ring) !== null)
        .map(point => point.getAttribute('aria-label')?.split(' · ')[0]);
    return {selected: named('.vs-chart-ring'), lineage: named('.vs-chart-lit')};
  });
}

test('the performance row is the same element, in the same place and size, at every level', async ({
  page,
}) => {
  await openRun(page);
  await expect(perfChart(page)).toBeVisible();
  await perfChart(page).evaluate(chart => chart.setAttribute('data-probe', 'perf'));
  const atRun = await perfRow(page);
  expect(atRun.probe).toBe('perf');
  expect(atRun.axes.length).toBeGreaterThan(0);
  const unchanged = async () => {
    await expect(perfChart(page)).toBeVisible();
    // The same node (not remounted), at the same place, size and axes, whatever opened below it.
    expect(await perfRow(page)).toEqual(atRun);
  };

  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await ideas.getByRole('option', {name: /Combine W1 ring with W2 padding/}).click();
  await expect(page.getByRole('heading', {name: 'Combine W1 ring with W2 padding'})).toBeVisible();
  await unchanged();
  await page
    .getByRole('listbox', {name: /steps$/})
    .getByRole('option', {name: /builder/i})
    .first()
    .click();
  await expect(page.getByRole('region', {name: 'Agent turn'})).toBeVisible();
  await unchanged();
  await page
    .getByRole('region', {name: 'Agent turn'})
    .getByRole('button', {name: /cargo bench --bench queue/})
    .last()
    .click();
  await expect(page.getByText('queue/mpmc time: 41.2 ms')).toBeVisible();
  await unchanged();
  for (const _level of ['item', 'step', 'idea']) await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox', {name: 'Running now'})).toBeVisible();
  await unchanged();
  // The run's other faces and the idea's Logs face keep it too.
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Logs').click();
  await unchanged();
});

test('an open idea is singled out on the chart: its reading ringed, the steps of its lineage ringed in the accent', async ({
  page,
}) => {
  await openRun(page);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  const key = page.getByRole('list', {name: 'Chart key'});
  expect(await ringed(page)).toEqual({selected: [], lineage: []});
  await expect(key.getByText('Selected', {exact: true})).toHaveCount(0);

  // A measured idea: its own reading is ringed, every reading stays, and the key names the ring.
  await ideas.getByRole('option', {name: /Cache-line padding/}).click();
  const open = await ringed(page);
  expect(open.selected).toEqual(['Cache-line padding and batched enqueue']);
  await expect(key.getByText('Selected', {exact: true})).toBeVisible();
  await expect(perfChart(page).getByRole('button')).toHaveCount(3);
  // The hero keeps the run's best.
  await expect(page.getByText('1.18×').first()).toBeVisible();

  // An idea with no measurement has no reading to ring, so the key does not name a ring.
  await page.keyboard.press('Escape');
  await ideas.getByRole('option', {name: /Combine W1 ring with W2 padding/}).click();
  expect((await ringed(page)).selected).toEqual([]);
  await expect(key.getByText('Selected', {exact: true})).toHaveCount(0);

  // Back at the run nothing is ringed.
  await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox', {name: 'Running now'})).toBeVisible();
  expect(await ringed(page)).toEqual({selected: [], lineage: []});
});

test('clicking a dot of the chart opens its idea', async ({page}) => {
  await openRun(page);
  await perfChart(page)
    .getByRole('button', {name: /^Cache-line padding and batched enqueue ·/})
    .click();
  await expect(
    page.getByRole('heading', {name: 'Cache-line padding and batched enqueue'}),
  ).toBeVisible();
  await expect(page).toHaveURL(/&idea=[^&]+$/);
  // The chart stays; another dot opens another idea beside the same chart.
  await perfChart(page)
    .getByRole('button', {name: /^Larger batch size ·/})
    .click();
  await expect(page.getByRole('heading', {name: 'Larger batch size'})).toBeVisible();
});

/** Every box of an idea's pane header: its kicker, title, facts and buttons, pairwise apart. */
async function headerBoxes(page: Page) {
  return page
    .getByRole('region', {name: 'Cache-line padding and batched enqueue'})
    .evaluate(pane => {
      const header = pane.querySelector('.vs-pane-header');
      if (header === null) throw new Error('no pane header');
      const leaves = [
        ...header.querySelectorAll(
          '.vs-pane-titles > .vs-text, .vs-pane-title, .vs-fact, .vs-pane-actions .vs-button',
        ),
      ];
      const frame = header.getBoundingClientRect();
      return leaves.map(leaf => {
        const {x, y, width, height} = leaf.getBoundingClientRect();
        return {
          text: leaf.textContent ?? '',
          fact: leaf.classList.contains('vs-fact'),
          // What it draws (a cut line is wider inside than out), and the header it must stay in.
          clipped: leaf.scrollWidth > leaf.clientWidth + 1,
          inside: x >= frame.left - 0.5 && x + width <= frame.right + 0.5,
          x,
          y,
          width,
          height,
        };
      });
    });
}

/** Nothing overlaps anything: every pair of boxes is apart horizontally or vertically. */
function expectApart(boxes: Awaited<ReturnType<typeof headerBoxes>>, width: number): void {
  for (const [index, one] of boxes.entries()) {
    for (const other of boxes.slice(index + 1)) {
      const apart =
        one.x + one.width <= other.x + 0.5 ||
        other.x + other.width <= one.x + 0.5 ||
        one.y + one.height <= other.y + 0.5 ||
        other.y + other.height <= one.y + 0.5;
      expect(apart, `${one.text} overlaps ${other.text} at ${width}px`).toBe(true);
    }
  }
}

const LONG_BASE =
  'Replace the per-slot sequence numbers of the lock-free ring with a contiguous payload arena and batched cursor publication';

test('an idea’s header stays on clean lines without lineage at 900 and 1500 px', async ({page}) => {
  const long = JSON.parse(
    JSON.stringify(SERVER).replaceAll('Lock-free ring with per-slot sequence numbers', LONG_BASE),
  ) as RunFixture;
  await openRun(page, false, long);
  await page
    .getByRole('listbox', {name: 'Ideas'})
    .getByRole('option', {name: /Cache-line padding/})
    .click();
  for (const width of [900, 1500]) {
    await page.setViewportSize({width, height: 900});
    await expect(page.getByText('0.91× vs base')).toHaveCount(0);
    await expect(page.getByText(/^from$/)).toHaveCount(0);
    const boxes = await headerBoxes(page);
    expect(boxes.length).toBeGreaterThan(2);
    expectApart(boxes, width);
    // Everything stays inside the header, and a fact that must be whole is not cut.
    for (const box of boxes)
      expect(box.inside, `${box.text} leaves the header at ${width}px`).toBe(true);
    for (const box of boxes.filter(each => each.fact && /^(Passed|\d+m)$/.test(each.text))) {
      expect(box.clipped, `${box.text} is cut at ${width}px`).toBe(false);
    }
    // The performance row keeps room for its chart whatever the title lengths.
    expect((await perfRow(page)).chart.width).toBeGreaterThan(200);
    // Every figure and label is one line high (a small button is 32px, a wrapped line pair 40+).
    for (const box of boxes.filter(each => /^(Passed|\d+m)$/.test(each.text))) {
      expect(box.height, `${box.text} wraps at ${width}px`).toBeLessThanOrEqual(34);
    }
    // The outcome and duration stay on one line.
    const facts = boxes.filter(each => each.fact);
    expect(facts.length).toBe(2);
    if (width >= 1500) {
      expect(
        Math.max(...facts.map(each => each.y)) - Math.min(...facts.map(each => each.y)),
      ).toBeLessThan(12);
    }
    if (shots !== undefined) {
      await page.getByRole('main').screenshot({path: `${shots}/ink-idea-${width}.png`});
    }
  }
});

test('the scrubber replays the run’s ideas at an earlier event and returns to live', async ({
  page,
}) => {
  await openRun(page);
  await showList(page);
  const ideas = page.getByRole('listbox', {name: 'Ideas'});
  await expect(ideas.getByRole('option')).toHaveCount(7);
  await page.getByRole('slider', {name: 'Run position'}).focus();
  await page.keyboard.press('Home');
  await expect(page.getByText('No ideas yet')).toBeVisible();
  await page.getByRole('button', {name: 'Back to live'}).click();
  await expect(ideas.getByRole('option')).toHaveCount(7);
  await page.evaluate(
    ([value]) => (window as unknown as FakeHostWindow).fakeHost.listing(value),
    [
      listed(
        [],
        [server('stopped', {outcome: 'stopped', reason: 'Stopped by you.', resumable: false})],
      ),
    ],
  );
  await transitionJourney(page, 'trace', 'stopped');
  await expect(
    page.getByRole('main').locator('.vs-chip', {hasText: 'Stopped'}),
  ).toHaveAccessibleDescription('Stopped by you.');
  await check(page, 'ink-run-ended');
  // The Logs tab is a zoom the address follows too.
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Logs').click();
  await expect(page).toHaveURL(/&mode=logs$/);
  await page.goBack();
  await expect(page).toHaveURL(RUN_LEVEL);
});

test('the Logs face lists the run’s streams by owner, and an idea shows only its own', async ({
  page,
}) => {
  await openRun(page);
  const show = page.getByRole('radiogroup', {name: 'Show'});
  await show.getByText('Logs').click();
  const streams = page.getByRole('listbox', {name: 'Log streams'});
  await expect(streams.getByRole('option')).toHaveCount(2);
  await streams.getByRole('option', {name: /benchmark/}).click();
  const log = page.getByRole('log', {name: /benchmark/});
  await expect(log).toContainText('repetition 1: 41.2 Mops/s');
  await check(page, 'ink-logs-run');

  // The idea that owns the benchmark gate shows that stream alone: no list, one pane.
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Ideas').click();
  await page
    .getByRole('listbox', {name: 'Ideas'})
    .getByRole('option', {name: /Combine W1 ring with W2 padding/})
    .click();
  await page
    .locator('.vs-pane[data-width="fill"]')
    .getByRole('radiogroup', {name: 'Show'})
    .getByText('Logs')
    .click();
  await expect(page.getByRole('log', {name: /benchmark/})).toContainText('repetition 1');
  await expect(page.getByRole('listbox', {name: 'Log streams'})).toHaveCount(0);
  await check(page, 'ink-logs-idea');
});

test('the timeline draws one row per idea; a segment opens its step, a row its idea', async ({
  page,
}) => {
  await openRun(page);
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Timeline', {exact: true}).click();
  const gantt = page.getByRole('listbox', {name: 'Ideas on the timeline'});
  // The Ideas list's order: running first, then the rest newest first.
  await expect(gantt.getByRole('option')).toHaveText([
    /Per-core queues with stealing/,
    /Wait-free consumer path/,
    /Combine W1 ring with W2 padding/,
    /Larger batch size/,
    /Spin then park/,
    /Cache-line padding and batched enqueue/,
    /Lock-free ring with per-slot sequence numbers/,
  ]);
  const range = page.getByRole('radiogroup', {name: 'Range'});
  await range.getByText('Last hour').click();
  await expect(range.getByLabel('Last hour')).toBeChecked();
  await range.getByText('All', {exact: true}).click();
  await check(page, 'ink-timeline');

  // A segment opens its step, beside the idea's steps.
  await gantt
    .getByRole('option', {name: /Combine W1 ring/})
    .locator('.vs-gantt-seg')
    .first()
    .click();
  await expect(page.getByRole('listbox', {name: /steps$/})).toBeVisible();
  await page.keyboard.press('Escape');

  // Up from a step is its idea: its steps and the strip of them over time.
  await expect(
    page.getByRole('listbox', {name: 'Combine W1 ring with W2 padding steps', exact: true}),
  ).toBeVisible();
  await expect(page.getByRole('listbox', {name: /steps over time$/})).toBeVisible();
  await check(page, 'ink-idea-steps');

  // A row opens its idea.
  await page.keyboard.press('Escape');
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Timeline', {exact: true}).click();
  await gantt.getByRole('option', {name: /Wait-free consumer path/}).click();
  await expect(page.getByRole('heading', {name: 'Wait-free consumer path'})).toBeVisible();
});

test('an idea’s Logs face shows its stream without a pick: the page follows what it shows', async ({
  page,
}) => {
  await openRun(page);
  await page
    .getByRole('listbox', {name: 'Ideas'})
    .getByRole('option', {name: /Combine W1 ring with W2 padding/})
    .click();
  await page
    .locator('.vs-pane[data-width="fill"]')
    .getByRole('radiogroup', {name: 'Show'})
    .getByText('Logs')
    .click();
  await expect(page.getByRole('log', {name: /benchmark/})).toContainText('repetition 1: 41.2');
});

test('every face of the run level keeps the time bar and the hero in view', async ({page}) => {
  await openRun(page);
  const show = page.getByRole('radiogroup', {name: 'Show'});
  for (const face of ['Ideas', 'Timeline', 'Agents', 'Logs']) {
    await show.getByText(face, {exact: true}).click();
    await expect(show.getByLabel(face, {exact: true})).toBeChecked();
    await expect(page.getByRole('slider', {name: 'Run position'})).toBeInViewport();
    await expect(page.getByText('1.18×').first()).toBeInViewport();
    expect(
      await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight),
    ).toBe(true);
  }
  // Now keeps what runs whole (the role and what it does), even in a narrow window; the idea's name gives way.
  await page.setViewportSize({width: 1024, height: 800});
  const rows = page.getByRole('listbox', {name: 'Running now'}).getByRole('option');
  await expect(rows.first()).toBeVisible();
  for (const row of await rows.all()) {
    const doing = row.locator('.vs-pickrow-text > :nth-child(2)');
    await expect(doing).toHaveText(/\S/);
    expect(await doing.evaluate(each => each.scrollWidth <= each.clientWidth)).toBe(true);
  }
});

test('a live turn keeps its scroll position and its element as events arrive', async ({page}) => {
  await openRun(page, true);
  await page
    .getByRole('listbox', {name: 'Ideas'})
    .getByRole('option', {name: /Wait-free consumer path/})
    .click();
  await page
    .getByRole('listbox', {name: /steps$/})
    .getByRole('option', {name: /builder/i})
    .first()
    .click();
  let sequence = journeyFixture('live').liveAfter ?? 0;
  const more = (_from: number, count: number) => {
    const after = sequence;
    sequence += count;
    return appendJourney(page, after, sequence);
  };
  await more(0, 12);
  const turn = page.getByRole('region', {name: 'Agent turn'});

  // Thinking and text come first, and the reply shows every one of its fields, with no collapse.
  await expect(turn).toContainText('Said number 11');
  await expect(turn.getByRole('button', {name: /Show all fields/})).toHaveCount(0);
  expect(await turn.evaluate(element => element.textContent?.includes('**'))).toBe(false);

  // A live turn the reader has not scrolled away from stays at its end.
  const gap = () =>
    turn.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight);
  await expect.poll(gap).toBeLessThan(2);
  await more(12, 1);
  // The reader watches this thought being written: the page draws it as "Thinking" and keeps it
  // open from then on. Name it by its title, never by position: the turn draws only the thoughts
  // near the visible part, so "the first" is whichever one the window starts at, and a press that
  // scrolls moves the window. Only a thought drawn while it was written stays open, so the test
  // waits for this one to be drawn so, whatever the page's speed.
  const watched = turn.locator('.vs-thought', {hasText: /Thought title 12(?!\d)/});
  await expect(watched.and(turn.locator('[data-live]'))).toBeVisible();
  await expect.poll(gap).toBeLessThan(2);

  // Scrolled away, it stays where it is: the same element, and no scroll event moved it. (The
  // reasoning is closed to a line, so the turn is shorter than it was: scroll a short way up.)
  const AWAY = 150;
  await expect
    .poll(() => turn.evaluate(element => element.scrollHeight - element.clientHeight))
    .toBeGreaterThan(AWAY);
  await turn.evaluate((element, top) => {
    const seen: number[] = [];
    Object.assign(window, {scrolls: seen});
    element.setAttribute('data-probe', '1');
    element.addEventListener('scroll', () => seen.push(element.scrollTop));
    element.scrollTop = top;
  }, AWAY);
  const scrolls = () => page.evaluate(() => (window as unknown as {scrolls: number[]}).scrolls);
  // The page has taken the reader's scroll before the run updates.
  await expect.poll(async () => (await scrolls()).length).toBeGreaterThan(0);
  const height = () => turn.evaluate(element => element.scrollHeight);
  const before = await height();
  await more(13, 4);
  await expect.poll(height).toBeGreaterThan(before);
  await more(17, 1);
  await expect(turn.locator('xpath=self::*[@data-probe]')).toHaveCount(1);
  expect((await scrolls()).filter(top => top !== AWAY)).toEqual([]);
  expect(await turn.evaluate(element => element.scrollTop)).toBe(AWAY);

  // A link in what the agent said opens through the app's opener, never in the window.
  const url = page.url();
  await turn.getByRole('link', {name: 'the notes 11'}).click();
  await expect
    .poll(() =>
      page.evaluate(
        () => (window as unknown as {fakeHost: {windows: FakeWindows}}).fakeHost.windows.external,
      ),
    )
    .toEqual(['https://example.com/notes/11']);
  expect(page.url()).toBe(url);
  await more(0, 1); // The real turn closes only after its last transcript chunk.
  await expect(turn).toContainText('Padded both counters');
  await expect(turn.getByRole('button', {name: /Show all fields/})).toHaveCount(0);

  // The turn is over. The reader had that thought in view while it was written, so it stays open
  // rather than collapsing under them; its bold title is a title, never markup.
  const line = watched.getByRole('button');
  await expect(line).toHaveAttribute('aria-expanded', 'true');
  await expect(watched.locator('strong').first()).toBeVisible();
  // A press closes it to a one-line preview, and it stays closed.
  await line.click();
  await expect(line).toHaveAttribute('aria-expanded', 'false');
  await expect(watched.locator('.vs-collapsible-summary')).toBeVisible();
  expect(await turn.evaluate(element => element.textContent?.includes('**'))).toBe(false);
});
