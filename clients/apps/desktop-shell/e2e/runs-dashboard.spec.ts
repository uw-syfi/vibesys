import {expect, type Page, test} from '@playwright/test';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {dashboardWorld, ONE_RUN, open, type RunCase} from './dashboard-world.js';
import {installGateway, wire} from './fake-gateway.js';
import {sidebar} from './read.js';

/**
 * What the Runs dashboard shows is held in a DOM (`src/runs-dashboard.dom.test.tsx`). The browser
 * keeps what needs a browser: how the tiles sit (density, overlap), and the two walks that open a
 * run from a tile.
 */

test.use({viewport: {width: 1440, height: 900}});

const group = (page: Page, name: string) => page.getByRole('region', {name, exact: true});
const tiles = (page: Page, name: string) => group(page, name).locator('.vs-overview-tile');

test('a tile opens its run, and the rail keeps Runs as where it came from', async ({page}) => {
  await open(page, dashboardWorld(ONE_RUN));

  await tiles(page, 'Running')
    .first()
    .getByRole('button', {name: /^queue-rs-run1/})
    .click();

  await expect(page.getByRole('heading', {name: 'Runs'})).toHaveCount(0);
  const rail = sidebar(page);
  await expect(rail.getByRole('button', {name: /^Runs/})).toHaveAttribute('aria-current', 'page');
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
  const tile = tiles(page, 'Unknown');
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
