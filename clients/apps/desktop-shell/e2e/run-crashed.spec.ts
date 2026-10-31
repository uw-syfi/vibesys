import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {openFixture} from './fixture-world.js';

/**
 * A real run that crashed after its implementer turn finished, as the Run screen shows it: nothing
 * it left open reads as running, the turn and the idea last as long as they did, the baseline's
 * failed gates read as `input not measured` on the figures, and the banner gives the whole failure. `RUN_SHOTS` names a directory to save the app window in.
 */
const shots = process.env['RUN_SHOTS'];

async function shot(page: Page, name: string): Promise<void> {
  if (shots !== undefined) await page.screenshot({path: `${shots}/${name}.png`});
}

test.use({colorScheme: 'light', viewport: {width: 1440, height: 900}});

test('the idea, the turn and the clock stop where the run did', async ({page}) => {
  const fixture = fixtureNamed('crashed-after-turn');
  await openFixture(page, fixture);
  const main = page.getByRole('main');
  await expect(main.getByText('Running', {exact: true})).toHaveCount(0);
  await main
    .locator('.vs-banner')
    .filter({hasText: 'Run diagnostic'})
    .getByRole('button', {name: 'Show details'})
    .click();
  await expect(main.getByText(/run failed/i).first()).toBeVisible();
  await expect(main.getByText('input not measured')).toBeVisible();
  await shot(page, 'ideas');
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
  const implementer = page
    .getByRole('listbox', {name: 'Agents'})
    .getByRole('option', {name: /Implementer/});
  await expect(implementer).toContainText(/2[89]m/);
  await expect(implementer).not.toContainText('0s');
  await shot(page, 'agents');
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Timeline').click();
  await shot(page, 'timeline');
});

test('the banner gives the whole reason the run failed', async ({page}) => {
  const fixture = fixtureNamed('crashed-after-turn');
  await openFixture(page, fixture);
  await page
    .getByRole('main')
    .locator('.vs-banner')
    .filter({has: page.getByRole('button', {name: 'Run again'})})
    .getByRole('button', {name: 'Show details'})
    .click();
  // The header's facts and the banner each give it whole; the first match is the whole reason.
  await expect(
    page
      .getByRole('main')
      .getByText(fixture.end?.reason ?? '')
      .first(),
  ).toBeVisible();
});
