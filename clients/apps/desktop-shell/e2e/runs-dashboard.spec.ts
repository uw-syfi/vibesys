import {expect, type Page, test} from '@playwright/test';
import {
  dashboardWorld,
  listingOf,
  MANY_RUNS,
  NOW,
  OMITTED_RUN,
  ONE_RUN,
  open,
  type RunCase,
} from './dashboard-world.js';
import type {FakeHostWindow} from './world.js';

/**
 * The Runs dashboard's journeys against the Fake bridge: every run of every machine as a tile,
 * grouped and regrouped, the runs that need the user in their own row and on the rail's badge, a
 * tile opening its run, archiving it, and a machine that cannot be reached or listed.
 */

test.use({viewport: {width: 1440, height: 900}});

const group = (page: Page, name: string) => page.getByRole('region', {name, exact: true});
const tiles = (page: Page, name: string) => group(page, name).locator('.vs-overview-tile');

async function groupTitles(page: Page): Promise<string[]> {
  const titles = await page
    .locator('.vs-overview-group-head')
    .evaluateAll(heads =>
      heads.map(head => [...head.children].map(child => child.textContent ?? '').join(' ')),
    );
  return titles.map(text => text.replace(/\s+/g, ' ').trim());
}

test('groups every run of every machine by status, the runs that need the user first', async ({
  page,
}) => {
  await open(page, dashboardWorld(MANY_RUNS));

  expect(await groupTitles(page)).toEqual([
    'Needs you 2',
    'Running 2',
    'Paused 1',
    'Finished 3',
    'Stopped 1',
  ]);
  await expect(tiles(page, 'Needs you')).toHaveCount(2);
  const stalled = tiles(page, 'Needs you').filter({hasText: 'mpsc'});
  await expect(stalled).toContainText('host-b');
  await expect(stalled).toContainText('stalled 32m');
  await expect(tiles(page, 'Needs you').filter({hasText: 'spsc'})).toContainText('failed');
  // A run that needs the user is in that group alone.
  await expect(page.locator('.vs-overview-tile')).toHaveCount(9);
});

test('a tile says its result against the base, where it ran, its ideas and time, and charts every idea', async ({
  page,
}) => {
  await open(page, dashboardWorld(ONE_RUN));

  const tile = tiles(page, 'Running').first();
  await expect(tile).toContainText('queue-rs-run1');
  await expect(tile).toContainText('mpmc');
  await expect(tile).toContainText('1.42×');
  await expect(tile).toContainText('host-a');
  await expect(tile).toContainText('7 ideas · 40m');
  await expect(tile).toContainText('running');
  const marks = tile.locator('.vs-result-chart-mark');
  await expect(marks).toHaveCount(7);
  await expect(tile.locator('[data-mark="kept"]')).toHaveCount(3);
  await expect(tile.locator('[data-mark="not-better"]')).toHaveCount(2);
  await expect(tile.locator('[data-mark="failed"]')).toHaveCount(1);
  await expect(tile.locator('[data-mark="pending"]')).toHaveCount(1);
  await expect(tile.locator('.vs-result-chart-base')).toHaveCount(1);
});

test('a live run whose input was never measured headlines its best measured value', async ({
  page,
}) => {
  const run: RunCase = {
    ...(ONE_RUN[0] as RunCase),
    points: [
      [null, 'kept'],
      [null, 'running'],
    ],
    best: {value: 287.4, metric: 'output_tokens_per_s', unit: null},
  };
  await open(page, dashboardWorld([run]));

  const tile = tiles(page, 'Running').first();
  await expect(tile).toContainText('287');
  await expect(tile).toContainText('output tokens/s');
  await expect(tile).not.toContainText('×');
});

test('a run that needs the user says why in a few words, whole words, with the full text on hover', async ({
  page,
}) => {
  const full = 'NestedRepositoryError: before the run could begin it found a repository inside';
  const long = 'the agent was interrupted because the machine went away during the second round';
  const failing = (runId: string, name: string, endSummary: string): RunCase => ({
    runId,
    name,
    host: 'ssh:host-a',
    task: 'mpmc',
    phase: 'failed',
    ago: 3,
    attention: 'failed',
    endSummary,
    points: [[null, 'failed']],
  });
  await open(
    page,
    dashboardWorld([
      failing('r-fail-01', 'queue-rs-a', full),
      failing('r-fail-02', 'queue-rs-b', long),
    ]),
  );

  const notes = tiles(page, 'Needs you').locator('.vs-overview-tile-note');
  const shown = await notes.evaluateAll(each =>
    each.map(note => ({text: note.textContent ?? '', title: note.getAttribute('title') ?? ''})),
  );
  expect(shown.map(note => note.text).sort()).toEqual([
    'Nested repository error',
    'the agent was interrupted because…',
  ]);
  expect(shown.map(note => note.title).sort()).toEqual([full, long].sort());
});

test('a run with more ideas than its chart keeps still counts them all', async ({page}) => {
  await open(page, dashboardWorld(OMITTED_RUN));

  const tile = tiles(page, 'Running').first();
  await expect(tile).toContainText('100 ideas');
  await expect(tile.locator('.vs-result-chart-mark')).toHaveCount(64);
});

test('Group by regroups the same tiles by machine, task and agent', async ({page}) => {
  await open(page, dashboardWorld(MANY_RUNS));
  const by = page.getByRole('combobox', {name: 'Group by'});
  const names = async (): Promise<string[]> =>
    (await groupTitles(page)).filter(title => !title.startsWith('Needs you'));

  await by.selectOption('machine');
  expect(await names()).toEqual(['host-a 4', 'host-b 3']);
  await by.selectOption('task');
  expect(await names()).toEqual(['mpmc 3', 'mpsc 2', 'spsc 2']);
  await by.selectOption('agent');
  expect(await names()).toEqual(['Unknown 7']);
  await by.selectOption('status');
  expect((await names())[0]).toBe('Running 2');
  // Needs you is its own group whatever the grouping.
  await expect(tiles(page, 'Needs you')).toHaveCount(2);
});

test('the rail counts the runs that need the user, and Runs and the mark both go home', async ({
  page,
}) => {
  await open(page, dashboardWorld(MANY_RUNS));
  const rail = page.getByRole('navigation', {name: 'VibeSys'});

  await expect(rail.getByRole('button', {name: /^Runs/})).toHaveAccessibleName(/2 need you/);
  await rail.getByRole('button', {name: 'New', exact: true}).click();
  await expect(page.getByRole('heading', {name: 'Runs'})).toHaveCount(0);
  await rail.getByRole('button', {name: 'VibeSys'}).click();
  await expect(page.getByRole('heading', {name: 'Runs'})).toBeVisible();
  await expect(rail.getByRole('button', {name: /^Runs/})).toHaveAttribute('aria-current', 'page');
});

test('a tile opens its run, and the rail keeps Runs as where it came from', async ({page}) => {
  await open(page, dashboardWorld(ONE_RUN));

  await tiles(page, 'Running')
    .first()
    .getByRole('button', {name: /^queue-rs-run1/})
    .click();

  await expect(page.getByRole('heading', {name: 'Runs'})).toHaveCount(0);
  const rail = page.getByRole('navigation', {name: 'VibeSys'});
  await expect(rail.getByRole('button', {name: /^Runs/})).toHaveAttribute('aria-current', 'page');
});

test('Archive takes a run off the dashboard', async ({page}) => {
  await open(page, dashboardWorld(MANY_RUNS));
  await expect(tiles(page, 'Stopped')).toHaveCount(1);

  await page.getByRole('button', {name: 'More for queue-rs-run9'}).click();
  await page.getByRole('menuitem', {name: 'Archive'}).click();

  await expect(page.getByRole('region', {name: 'Stopped', exact: true})).toHaveCount(0);
});

interface Box {
  readonly left: number;
  readonly top: number;
  readonly right: number;
  readonly bottom: number;
}

/** Each tile of the group named `name`, as the page lays it out. */
async function boxes(page: Page, name: string): Promise<Box[]> {
  return tiles(page, name).evaluateAll(each =>
    each.map(tile => {
      const {left, top, right, bottom} = tile.getBoundingClientRect();
      return {left, top, right, bottom};
    }),
  );
}

/** Eight runs of one machine, so one group holds more tiles than any row can. */
const CROWD: readonly RunCase[] = Array.from({length: 8}, (_, index) => ({
  ...ONE_RUN[0],
  runId: `r-crowd-${10 + index}`,
  name: `queue-rs-crowd${index}`,
  ago: 1 + index,
})) as readonly RunCase[];

test('tiles are dense: at 1440 four or more share a row, and at 660 none overlap', async ({
  page,
}) => {
  await open(page, dashboardWorld(CROWD));

  const wide = await boxes(page, 'Running');
  expect(wide).toHaveLength(8);
  const first = Math.min(...wide.map(box => box.top));
  expect(wide.filter(box => box.top === first).length).toBeGreaterThanOrEqual(4);
  for (const box of wide) expect(box.bottom - box.top).toBeLessThanOrEqual(160);

  await page.setViewportSize({width: 660, height: 900});
  const narrow = await boxes(page, 'Running');
  for (const [index, a] of narrow.entries()) {
    for (const b of narrow.slice(index + 1)) {
      const apart =
        a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top;
      expect(apart, 'two tiles overlap').toBe(true);
    }
  }
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

test('a machine that cannot list a project’s runs still shows the runs its registry reports', async ({
  page,
}) => {
  const world = dashboardWorld(MANY_RUNS);
  const rest = Object.fromEntries(
    Object.entries(world.projectRuns ?? {}).filter(([key]) => !key.startsWith('ssh:host-b|')),
  );
  await open(page, {...world, projectRuns: rest});

  // The runs the machine itself reports (live and recently ended) need no project listing.
  await expect(page.locator('.vs-overview-tile', {hasText: 'host-b'})).toHaveCount(4);
  await expect(page.locator('.vs-overview-tile', {hasText: 'host-a'})).toHaveCount(5);
});

test('a host that sends no chart data still shows each run as a tile without a chart', async ({
  page,
}) => {
  const world = dashboardWorld(ONE_RUN);
  const digests = Object.fromEntries(
    Object.entries(world.digests ?? {}).map(([id, entry]) => {
      const {dashboard: _chart, ...plain} = entry as Record<string, unknown>;
      return [id, plain];
    }),
  );
  await open(page, {...world, digests});

  const tile = tiles(page, 'Running').first();
  await expect(tile).toContainText('mpmc');
  await expect(tile.locator('.vs-result-chart')).toHaveCount(0);
});

test('Machines carries a warning dot while a machine cannot be reached', async ({page}) => {
  const world = dashboardWorld(ONE_RUN as readonly RunCase[]);
  await open(page, {...world, reports: {'ssh:host-b': {kind: 'unavailable', detail: 'down'}}});
  const rail = page.getByRole('navigation', {name: 'VibeSys'});

  await expect(rail.getByRole('button', {name: /^Machines/})).toHaveAccessibleName(/Unreachable/);
});

test('a run that ends moves from Running to Finished', async ({page}) => {
  await open(page, dashboardWorld(ONE_RUN));
  await expect(tiles(page, 'Running')).toHaveCount(1);

  const finished = [{...ONE_RUN[0], phase: 'completed'} as RunCase];
  const nextDigests = dashboardWorld(finished).digests;
  await page.evaluate(
    digests =>
      (window as unknown as {fakeRunDigests: {set(value: unknown): void}}).fakeRunDigests.set(
        digests,
      ),
    nextDigests,
  );
  const ended = listingOf(finished, 'ssh:host-a');
  await page.evaluate(
    ([listing]) => (window as unknown as FakeHostWindow).fakeHost.listing(listing),
    [ended],
  );

  await expect(group(page, 'Running')).toHaveCount(0);
  await expect(tiles(page, 'Finished')).toHaveCount(1);
  await expect(tiles(page, 'Finished').first()).toContainText('finished');
});

test('on a machine whose VibeSys is too old for digests, a run still says its own status', async ({
  page,
}) => {
  const world = dashboardWorld(MANY_RUNS, ['ssh:host-b']);
  const key = Object.keys(world.projectRuns ?? {}).find(each => each.startsWith('ssh:host-b'));
  if (key === undefined) throw new Error('the world lists nothing on host-b');
  // A run the registry no longer holds: only a digest could say how it went.
  const old = {runId: 'r-old-99', name: 'r-old-99', task: 'mpmc', createdAt: NOW - 50 * 3_600_000};
  await open(page, {
    ...world,
    projectRuns: {...world.projectRuns, [key]: [...(world.projectRuns?.[key] ?? []), old]},
  });

  // host-b's finished runs come from the registry's record of their end, not from a digest.
  const finished = tiles(page, 'Finished').filter({hasText: 'host-b'});
  await expect(finished).toHaveCount(2);
  await expect(finished.first()).toContainText('finished');
  await expect(finished.first()).not.toContainText('newer VibeSys');
  // A run only a digest could place is unknown, with the machine's trouble beside it.
  const unknown = tiles(page, 'Unknown').first();
  await expect(unknown).toContainText('unknown');
  await expect(unknown).toContainText('needs an update');
});

test('an addressless run remains visible when its project is unknown', async ({page}) => {
  const world = dashboardWorld([]);
  await open(page, {
    ...world,
    listings: [
      {
        kind: 'listed',
        host: 'ssh:host-a',
        live: [],
        ended: [],
        unreadable: [],
        unverified: [],
        unknownProject: [
          {
            id: 'run-addressless',
            runId: 'run-addressless',
            status: 'running',
            phase: 'running',
            paused: null,
            end: null,
            startedAt: 0,
            reason: null,
          },
        ],
      },
    ],
    recent: [],
    projectRuns: {},
  });
  const tile = tiles(page, 'Unknown');
  await expect(tile).toHaveCount(1);
  await expect(tile).toBeVisible();
  await expect(tile).toContainText('Unknown project');
  await expect(tile).toContainText('host-a');
  await expect(tile).toContainText('Project unknown');
  const shots = process.env['SCREENS_DIR'];
  if (shots !== undefined)
    await page.screenshot({path: `${shots}/runs__unknown-project__1440.png`});
  await expect(page.getByRole('heading', {name: 'Runs', level: 1})).toBeVisible();
});
