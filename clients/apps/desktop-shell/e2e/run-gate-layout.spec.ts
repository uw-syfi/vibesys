import {mkdirSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * A live run whose input benchmark failed before the first idea: nothing stands between the
 * figures and the lists. Regression: a card for the failed
 * gate pushed the lists to the lower third of the window and left an empty band under the figures.
 * `SCREENS_DIR=<dir>` writes each screenshot (the page only).
 */

const FIXTURE = 'input-gate-failed';
const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

for (const width of [1000, 1440]) {
  test(`the failed input gate has no card and the lists keep their room, at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({width, height: 900});
    const fixture = fixtureNamed(FIXTURE);
    await openFixture(page, fixture);
    await playLive(page, fixture);
    await settled(page);
    await expect(page.locator('.vs-check-list')).toHaveCount(0);
    await expect(page.getByText(/Best so far|Adopted:/)).toHaveCount(0);
    if (shots !== undefined) await page.screenshot({path: `${shots}/run-gate__${width}.png`});
    const listsHeight = await page.evaluate(
      () => document.querySelector('.vs-panes')?.getBoundingClientRect().height ?? 0,
    );
    expect(listsHeight, 'the lists keep their room').toBeGreaterThanOrEqual(900 * 0.38);
  });
}
