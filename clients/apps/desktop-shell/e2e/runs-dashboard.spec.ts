import {expect, type Page, test} from '@playwright/test';
import {storeRunFixture} from '@vibesys/run-fixtures';
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
import {installGateway, wire} from './fake-gateway.js';
import type {FakeHostWindow} from './world.js';

/**
 * The Runs dashboard's journeys against the Fake bridge: every run of every machine as a tile,
 * grouped and regrouped, the runs that need the user as cards that say why and on the rail's badge,
 * a tile opening its run, a card running its run again, archiving a run, and a machine that cannot
 * be reached or listed.
 */

test.use({viewport: {width: 1440, height: 900}});

const group = (page: Page, name: string | RegExp) =>
  page.getByRole('region', {name, exact: typeof name === 'string'});
const tiles = (page: Page, name: string | RegExp) => group(page, name).locator('.vs-overview-tile');
/** The one group the runs that do not need the user make, grouped by status: `Running 2 · ...`. */
const BY_STATUS = /^(Running|Paused|Failed|Finished|Stopped|Unknown) \d/;

/** Each group's title (its region's name) and how many tiles it holds, in order. */
async function groupTitles(page: Page): Promise<string[]> {
  return page
    .locator('section.vs-overview-group')
    .evaluateAll(groups =>
      groups.map(
        each =>
          `${each.getAttribute('aria-label')} [${each.querySelectorAll('.vs-overview-tile').length}]`,
      ),
    );
}

test('groups every run of every machine by status, the runs that need the user first', async ({
  page,
}) => {
  await open(page, dashboardWorld(MANY_RUNS));

  expect(await groupTitles(page)).toEqual([
    'Needs you [2]',
    'Running 2 · Paused 1 · Finished 3 · Stopped 2 [8]',
  ]);
  const stalled = tiles(page, 'Needs you').filter({hasText: 'mpsc'});
  await expect(stalled).toContainText('host-b');
  await expect(stalled).toContainText('Quiet 32m');
  await expect(stalled).toContainText('Still running, but it has recorded nothing for 32m.');
  await expect(stalled.getByRole('button', {name: 'Run again with changes'})).toHaveCount(0);
  const failed = tiles(page, 'Needs you').filter({hasText: 'spsc'});
  await expect(failed).toContainText('Failed');
  await expect(failed).toContainText('All 3 ideas failed, so nothing beat the input.');
  // A run that needs the user is in that group alone.
  await expect(page.locator('.vs-overview-tile')).toHaveCount(10);
  // The rest are in status order, each saying where it stands.
  const lines = await tiles(page, BY_STATUS).locator('.vs-overview-tile-foot').allTextContents();
  expect(lines).toEqual([
    '1 idea working · 7 tried',
    '1 idea working · 2 tried',
    'Paused by you · 2 ideas tried',
    'Finished · 6 ideas in 40m',
    'Finished · 3 ideas in 40m',
    'Finished · 1 idea in 40m',
    'Stopped · 2 ideas in 12m',
    'Stopped · 1 idea in 40m',
  ]);
});

test('a tile says its best against the input, where it ran, what it is doing, and charts the best so far', async ({
  page,
}) => {
  await open(page, dashboardWorld(ONE_RUN));

  const tile = tiles(page, BY_STATUS).first();
  await expect(tile).toContainText('queue-rs-run1 · mpmc');
  await expect(tile).toContainText('host-a · Oct 17');
  await expect(tile).toContainText('1.42×best vs input');
  await expect(tile).toContainText('1 idea working · 7 tried');
  await expect(tile.getByRole('img', {name: 'running'})).toHaveCount(1);
  await expect(
    tile.getByRole('img', {name: 'Best so far over 7 ideas, against the input'}),
  ).toHaveCount(1);
  await expect(tile.locator('.vs-spark-reference')).toHaveCount(1);
  await expect(page.getByRole('list', {name: 'Chart key'})).toHaveText(/best so far\s*input/);
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

  const tile = tiles(page, BY_STATUS).first();
  await expect(tile).toContainText('287output tokens/s, best');
  await expect(tile).not.toContainText('×');
  // The input was never measured, so there is no line to draw against it.
  await expect(tile.locator('.vs-spark')).toHaveCount(0);
});

test('a run that failed says why in its record’s words, and offers to run it again', async ({
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

  const why = await tiles(page, 'Needs you').locator('.vs-attention-card-why').allTextContents();
  expect(why.sort()).toEqual([
    'Nested repository error: before the run could begin it found a repository inside',
    'The agent was interrupted because the machine went away during the second round',
  ]);
  const card = tiles(page, 'Needs you').filter({hasText: 'queue-rs-a'});
  await card.getByRole('button', {name: 'Run again with changes'}).click();
  const rail = page.getByRole('navigation', {name: 'VibeSys'});
  await expect(rail.getByRole('button', {name: 'New', exact: true})).toHaveAttribute(
    'aria-current',
    'page',
  );
});

test('a run with more ideas than its chart keeps still counts them all', async ({page}) => {
  await open(page, dashboardWorld(OMITTED_RUN));

  const tile = tiles(page, BY_STATUS).first();
  await expect(tile).toContainText('Running · 100 ideas tried');
  await expect(
    tile.getByRole('img', {name: 'Best so far over 100 ideas, against the input'}),
  ).toHaveCount(1);
});

test('Group by regroups the same tiles by machine and task', async ({page}) => {
  await open(page, dashboardWorld(MANY_RUNS));
  const by = page.getByRole('combobox', {name: 'Group by'});
  const names = async (): Promise<string[]> =>
    (await groupTitles(page)).filter(title => !title.startsWith('Needs you'));

  await expect(by.locator('option')).toHaveText(['Status', 'Machine', 'Task']);
  await by.selectOption('machine');
  expect(await names()).toEqual(['host-a [4]', 'host-b [4]']);
  await by.selectOption('task');
  expect(await names()).toEqual(['mpmc [3]', 'mpsc [2]', 'spsc [3]']);
  await by.selectOption('status');
  expect(await names()).toEqual(['Running 2 · Paused 1 · Finished 3 · Stopped 2 [8]']);
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

  await tiles(page, BY_STATUS)
    .first()
    .getByRole('button', {name: /^queue-rs-run1/})
    .click();

  await expect(page.getByRole('heading', {name: 'Runs'})).toHaveCount(0);
  const rail = page.getByRole('navigation', {name: 'VibeSys'});
  await expect(rail.getByRole('button', {name: /^Runs/})).toHaveAttribute('aria-current', 'page');
});

test('Archive takes a run off the dashboard', async ({page}) => {
  await open(page, dashboardWorld(MANY_RUNS));
  await expect(page.locator('.vs-overview-tile', {hasText: 'queue-rs-run9'})).toHaveCount(1);

  await page.getByRole('button', {name: 'More for queue-rs-run9'}).click();
  await page.getByRole('menuitem', {name: 'Archive'}).click();

  await expect(page.locator('.vs-overview-tile', {hasText: 'queue-rs-run9'})).toHaveCount(0);
  await expect(group(page, 'Running 2 · Paused 1 · Finished 3 · Stopped 1')).toBeVisible();
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

  const wide = await boxes(page, 'Running 8');
  expect(wide).toHaveLength(8);
  const first = Math.min(...wide.map(box => box.top));
  expect(wide.filter(box => box.top === first).length).toBeGreaterThanOrEqual(4);
  for (const box of wide) expect(box.bottom - box.top).toBeLessThanOrEqual(200);

  await page.setViewportSize({width: 660, height: 900});
  const narrow = await boxes(page, 'Running 8');
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
  await expect(page.locator('.vs-overview-tile', {hasText: 'host-b'})).toHaveCount(5);
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

  const tile = tiles(page, BY_STATUS).first();
  await expect(tile).toContainText('mpmc');
  await expect(tile.locator('.vs-spark')).toHaveCount(0);
});

test('Machines carries a warning dot while a machine cannot be reached', async ({page}) => {
  const world = dashboardWorld(ONE_RUN as readonly RunCase[]);
  await open(page, {...world, reports: {'ssh:host-b': {kind: 'unavailable', detail: 'down'}}});
  const rail = page.getByRole('navigation', {name: 'VibeSys'});

  await expect(rail.getByRole('button', {name: /^Machines/})).toHaveAccessibleName(/Unreachable/);
});

test('a run that ends moves from Running to Finished', async ({page}) => {
  await open(page, dashboardWorld(ONE_RUN));
  await expect(tiles(page, 'Running 1')).toHaveCount(1);

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

  await expect(group(page, 'Running 1')).toHaveCount(0);
  await expect(tiles(page, 'Finished 1')).toHaveCount(1);
  await expect(tiles(page, 'Finished 1').first()).toContainText('Finished · 7 ideas in 40m');
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
  const finished = tiles(page, BY_STATUS).filter({hasText: 'host-b'}).filter({hasText: 'Finished'});
  await expect(finished).toHaveCount(2);
  await expect(finished.first()).not.toContainText('newer VibeSys');
  // A run only a digest could place is unknown, with the machine's trouble beside it.
  await expect(tiles(page, BY_STATUS).filter({hasText: 'Unknown · needs an update'})).toHaveCount(
    1,
  );
});

test('an addressless run remains visible when its project is unknown', async ({page}) => {
  const world = dashboardWorld([]);
  await installGateway(page, wire(storeRunFixture('live')));
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
  const tile = tiles(page, 'Unknown 1');
  await expect(tile).toHaveCount(1);
  await expect(tile).toBeVisible();
  await expect(tile).toContainText('Unknown project');
  await expect(tile).toContainText('host-a');
  await expect(tile).toContainText('Project unknown');
  const shots = process.env['SCREENS_DIR'];
  if (shots !== undefined)
    await page.screenshot({path: `${shots}/runs__unknown-project__1440.png`});
  await tile.getByRole('button', {name: /^Unknown project/}).click();
  await expect(page.getByText('Project unknown', {exact: true})).toBeVisible();
  for (const name of ['Resume', 'Pause', 'Pause now', 'Guidance'])
    await expect(page.getByRole('button', {name, exact: true})).toHaveCount(0);
});
