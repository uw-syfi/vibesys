import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {checkControls, controlsOf} from './checks/controls-act.js';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * Every control of the Run screen acts: on a finished run, with the run level, an
 * opened idea, and the Timeline and Agents faces shown, each control found by role is used once on
 * a fresh copy of the screen (the copies side by side in one browser) and must change what the screen shows (`checks/controls-act.ts`). A
 * tab that marks itself selected while the panes stay put, the defect this check was written for,
 * fails here by name.
 */

const WIDTH = 1440;

/** Where on the Run screen the controls are taken from. */
const STARTS: Readonly<Record<string, (page: Page) => Promise<void>>> = {
  'the run': async () => undefined,
  'an opened idea': async page => {
    await page.getByRole('listbox', {name: 'Ideas'}).getByRole('option').first().click();
    await expect(page.getByRole('listbox', {name: /steps$/})).toBeVisible();
  },
  'the Timeline face': async page => {
    await page.getByRole('radiogroup', {name: 'Show'}).getByText('Timeline').click();
    await expect(page.getByRole('radio', {name: 'Timeline'})).toBeChecked();
  },
  'the Agents face': async page => {
    await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
    await expect(page.getByRole('listbox', {name: 'Agents'})).toBeVisible();
  },
  'the Usage face': async page => {
    await page.getByRole('radiogroup', {name: 'Show'}).getByText('Usage').click();
    await expect(page.getByRole('table', {name: 'Usage by group'})).toBeVisible();
  },
};

async function reachScreen(page: Page, fixtureName: string, start: string): Promise<void> {
  const fixture = fixtureNamed(fixtureName);
  await page.setViewportSize({width: WIDTH, height: 900});
  await openFixture(page, fixture);
  await playLive(page, fixture);
  await expect(
    page.getByRole('listbox', {name: 'Ideas'}).getByRole('option').first(),
  ).toBeVisible();
  await STARTS[start]?.(page);
}

/** Controls that change nothing on this run by design, each with why. */
const INERT_BY_DESIGN: readonly {
  readonly start: RegExp;
  readonly name: RegExp;
  readonly why: string;
}[] = [
  {
    start: /opened idea/,
    name: /^Per-core queues with stealing/,
    why: 'the chart mark of the idea that is already open',
  },
  {
    start: /Timeline/,
    name: /^Last (?:hour|day)$/,
    why: 'the range only drops rows older than that, and this run lies within the hour',
  },
];

const CELLS: readonly string[] = [
  'the run',
  'an opened idea',
  'the Timeline face',
  'the Agents face',
  'the Usage face',
];

/** The Usage face needs a run whose turns report usage. */
const fixtureFor = (start: string): string =>
  start === 'the Usage face' ? 'finished-ten-ideas' : 'finished-run';

for (const start of CELLS) {
  const fixtureName = fixtureFor(start);
  test(`every control of ${start} acts on ${fixtureName}`, async ({
    context,
    browser,
    baseURL,
    viewport,
    colorScheme,
    contextOptions,
  }) => {
    const first = await context.newPage();
    await reachScreen(first, fixtureName, start);
    await settled(first);
    const exempt = INERT_BY_DESIGN.filter(each => each.start.test(start));
    const controls = (await controlsOf(first.getByRole('main'))).filter(
      control => !exempt.some(each => each.name.test(control.name)),
    );
    await first.close();
    expect(controls.length, 'the screen has controls').toBeGreaterThan(0);
    const found = await checkControls(
      browser,
      {...contextOptions, ...(baseURL === undefined ? {} : {baseURL}), viewport, colorScheme},
      controls,
      async page => {
        await reachScreen(page, fixtureName, start);
      },
      page => page.getByRole('main'),
    );
    const inert = found.filter((problem): problem is string => problem !== null);
    expect(inert, `controls that do nothing:\n${inert.join('\n')}`).toEqual([]);
  });
}
