import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed, objectivesRun, type RunFixture} from '@vibesys/run-fixtures';
import {scrolledToEnd, settled} from './checks/settle.js';
import {openFixture} from './fixture-world.js';

/**
 * The Ideas table of a run of one, two, three and six objectives, in the app: each objective that
 * moved has a cell in the narrow form too, as its multiple; a column past the fourth is counted in
 * a header; no row is taller than a few lines at any width; and the table follows the time bar.
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

async function scrubTo(page: Page, share: number): Promise<void> {
  await page.getByRole('slider', {name: 'Run position'}).evaluate((element, at) => {
    const input = element as HTMLInputElement;
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    set?.call(input, String(Math.round(Number(input.max) * at)));
    input.dispatchEvent(new Event('input', {bubbles: true}));
  }, share);
  await settled(page);
}

for (const {name, objectives, run} of RUNS) {
  for (const width of [1440, 1200]) {
    test(`${name} objective${objectives > 1 ? 's' : ''} at ${width}px: every objective that moved keeps its cell, and no row is tall`, async ({
      page,
    }) => {
      await page.setViewportSize({width, height: 900});
      await openFixture(page, run);
      const ideas = page.getByRole('listbox', {name: 'Ideas'});
      await expect(ideas.getByRole('option').first()).toBeVisible();
      await settled(page);
      const shown = Math.min(objectives, 4);
      // The objectives that moved: the six-objective run has one that never moves, so it has five.
      const moved = objectives === 6 ? 5 : objectives;
      const headers = await objectiveHeaders(page);
      expect(headers).toHaveLength(Math.min(moved, 4));
      const notes = await page.locator('.vs-columnlist-note').allTextContents();
      expect(notes).toEqual(moved > 4 ? [`+${moved - 4}`] : []);
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
      expect(shown).toBeGreaterThan(0);
      // No title wraps a character to a line: the tallest row stays within a few lines.
      const tallest = await ideas
        .getByRole('option')
        .evaluateAll(rows => Math.max(...rows.map(each => each.getBoundingClientRect().height)));
      expect(tallest).toBeLessThanOrEqual(120);
      // The page does not scroll sideways.
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth),
      ).toBeLessThanOrEqual(0);
    });

    test(`${name} objective${objectives > 1 ? 's' : ''} at ${width}px: the table and the stats follow the time bar`, async ({
      page,
    }) => {
      await page.setViewportSize({width, height: 900});
      await openFixture(page, run);
      const ideas = page.getByRole('listbox', {name: 'Ideas'});
      await expect(ideas.getByRole('option').first()).toBeVisible();
      await settled(page);
      const tile = async (label: string) =>
        (await page.locator('.vs-stat').filter({hasText: label}).first().innerText()).split('\n');
      /** The figure of the first tile, the ideas tried: the first whole number after its label. */
      const tried = async () =>
        Number(/\d+/.exec((await tile('Ideas tried')).join(' ').replace(/ideas tried/i, ''))?.[0]);
      const wholeRun = await ideas.getByRole('option').count();
      expect(await tried()).toBe(wholeRun);
      const spentAtEnd = (await tile('Tokens')).join(' ');
      await scrubTo(page, 0.6);
      await scrolledToEnd(page);
      const atScrub = await ideas.getByRole('option').count();
      expect(atScrub).toBeLessThan(wholeRun);
      expect(await tried()).toBe(atScrub);
      // What the run spent by then is not what it spent in all.
      if (!spentAtEnd.includes('Not reported'))
        expect((await tile('Tokens')).join(' ')).not.toBe(spentAtEnd);
      // The multiples are those of the readings by then: none beyond the best the chart names.
      const best = await page.locator('.vs-chart-figure').first().innerText();
      const figure = Number(/([\d.]+)×/.exec(best)?.[1]);
      const multiples = await ideas
        .getByRole('option')
        .first()
        .evaluate(row =>
          [...row.querySelectorAll('[data-column^="objective"]')]
            .filter(each => getComputedStyle(each).display !== 'none')
            .map(each => Number(/([\d.]+)×/.exec((each as HTMLElement).innerText)?.[1])),
        );
      expect(multiples[0] ?? Number.NaN).toBeGreaterThan(0);
      expect(figure).toBeGreaterThan(0);
    });
  }
}
