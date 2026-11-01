import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed, objectivesRun, type RunFixture} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture} from './fixture-world.js';

/**
 * The layout of the Ideas table of a run of one, two, three and six objectives, in the app: the
 * columns and the cells the width leaves (each objective that moved keeps its cell in the narrow
 * form), no row taller than a few lines, and no sideways scroll, at 1440 and at 1200 on one page.
 * What the table says (the column cap and its count, the multiples, the stats following the time
 * bar) is the feature's own: `features/run-page` `ideas-table-objectives.dom.test.tsx`,
 * `ideas-table-view.test.ts` and `run-stats.test.ts`, which kill every product mutation the
 * browser tests for it killed.
 */

const RUNS: readonly {
  readonly name: string;
  readonly objectives: number;
  readonly run: RunFixture;
}[] = [
  {name: 'one', objectives: 1, run: fixtureNamed('finished-ten-ideas')},
  {name: 'two', objectives: 2, run: fixtureNamed('finished-two-objectives')},
  {name: 'three', objectives: 3, run: objectivesRun(3)},
  {name: 'six', objectives: 6, run: objectivesRun(6)},
];

/** The objective headers of the table: the columns between Status and Agent time that name a measure. */
async function objectiveHeaders(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const heads = [...document.querySelectorAll('.vs-columnlist-headcell')].filter(
      each => getComputedStyle(each).display !== 'none',
    );
    const text = heads.map(each => each.textContent ?? '');
    const from = text.indexOf('Status') + 1;
    const to = text.indexOf('Agent time');
    return text
      .slice(from, to < 0 ? text.length : to)
      .filter(each => each !== 'vs input' && each !== 'Share of gain' && each !== 'Built on');
  });
}

for (const {name, objectives, run} of RUNS) {
  test(`${name} objective${objectives > 1 ? 's' : ''}: every objective that moved keeps its cell, and no row is tall, at every width`, async ({
    page,
  }) => {
    await page.setViewportSize({width: 1440, height: 900});
    await openFixture(page, run);
    const ideas = page.getByRole('listbox', {name: 'Ideas'});
    await expect(ideas.getByRole('option').first()).toBeVisible();
    // The objectives that moved: the six-objective run has one that never moves, so it has five.
    const moved = objectives === 6 ? 5 : objectives;
    for (const width of [1440, 1200]) {
      await page.setViewportSize({width, height: 900});
      await settled(page);
      expect(await objectiveHeaders(page)).toHaveLength(Math.min(moved, 4));
      // Each row says each shown objective as a multiple (or a dash), whatever the width.
      const cells = await ideas
        .getByRole('option')
        .first()
        .evaluate(row =>
          [...row.querySelectorAll('.vs-columnlist-cell')]
            .filter(each => getComputedStyle(each).display !== 'none')
            .map(each => (each as HTMLElement).innerText),
        );
      expect(cells.filter(text => /×/.test(text))).toHaveLength(Math.min(moved, 4));
      // No title wraps a character to a line: the tallest row stays within a few lines.
      const tallest = await ideas
        .getByRole('option')
        .evaluateAll(rows => Math.max(...rows.map(each => each.getBoundingClientRect().height)));
      expect(tallest).toBeLessThanOrEqual(120);
      // The page does not scroll sideways.
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
      ).toBeLessThanOrEqual(0);
    }
  });
}
