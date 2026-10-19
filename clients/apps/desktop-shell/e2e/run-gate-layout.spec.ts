import {mkdirSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {openFixture, playLive} from './fixture-world.js';

/**
 * A live run whose input benchmark failed before the first idea: the failure stands as a card above
 * the figures. Regression: the card pushed the lists below it to the lower third of the window and
 * left an empty band between the figures and the lists, and the run's id, as long as a real one,
 * was cut with an ellipsis. `SCREENS_DIR=<dir>` writes each screenshot (the page only).
 */

const FIXTURE = 'input-gate-failed';
const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

for (const width of [1000, 1440]) {
  test(`the failed input gate leaves no empty band and the run id whole, at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({width, height: 900});
    const fixture = fixtureNamed(FIXTURE);
    await openFixture(page, fixture);
    await playLive(page, fixture);
    await settled(page);
    await expect(page.getByText('Benchmark', {exact: true})).toBeVisible();
    if (shots !== undefined) await page.screenshot({path: `${shots}/run-gate__${width}.png`});
    const measured = await page.evaluate(runId => {
      const box = (element: Element | null) => element?.getBoundingClientRect() ?? null;
      const text = [...document.querySelectorAll<HTMLElement>('*')].find(
        element => element.children.length === 0 && element.textContent?.includes(runId),
      );
      const panes = document.querySelector('.vs-panes');
      const lists = box(panes);
      const card = box(document.querySelector('.vs-card'));
      const label = [...document.querySelectorAll<HTMLElement>('.vs-card *')].find(
        element => element.children.length === 0 && element.textContent === 'Benchmark',
      );
      return {
        cardTop:
          card !== null && label !== undefined
            ? label.getBoundingClientRect().top - card.top
            : null,
        idCut: text === undefined ? null : text.scrollWidth > text.clientWidth,
        idText: text?.textContent ?? null,
        listsHeight: lists?.height ?? null,
        screen: window.innerHeight,
      };
    }, fixture.runId);
    expect(measured.idText, 'the run id is on the screen').toContain(fixture.runId);
    expect(measured.idCut, 'the run id is not cut').toBe(false);
    expect(measured.cardTop, 'the card has no empty band above its label').toBeLessThanOrEqual(24);
    expect(measured.listsHeight ?? 0, 'the lists keep their room').toBeGreaterThanOrEqual(
      measured.screen * 0.38,
    );
  });
}
