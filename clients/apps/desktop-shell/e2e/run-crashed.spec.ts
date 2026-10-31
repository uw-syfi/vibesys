import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {openFixture} from './fixture-world.js';

/**
 * A real run that crashed after its implementer turn finished, as the Run screen shows it: nothing
 * it left open reads as running, the turn and the idea last as long as they did, the baseline's
 * failed gates read as `input not measured` on the figures, and the header gives the whole failure on its status. `RUN_SHOTS` names a directory to save the app window in.
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
  await expect(main.getByText('input not measured')).toBeVisible();
  await shot(page, 'ideas');
  // The run's diagnostic is a row of Logs, and the tab says there is one.
  await expect(main.getByRole('img', {name: 'Diagnostic'})).toHaveCount(1);
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Logs').click();
  await expect(
    main
      .getByRole('log')
      .getByText(/run failed/i)
      .first(),
  ).toBeVisible();
  await shot(page, 'logs');
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Agents').click();
  const implementer = page
    .getByRole('listbox', {name: 'Agents'})
    .getByRole('option', {name: /Builder/});
  await expect(implementer).toContainText(/2[89]m/);
  await expect(implementer).not.toContainText('0s');
  await shot(page, 'agents');
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Timeline').click();
  await shot(page, 'timeline');
});

test('the header gives a few words of why the run failed, and the whole reason on its status', async ({
  page,
}) => {
  const fixture = fixtureNamed('crashed-after-turn');
  await openFixture(page, fixture);
  const main = page.getByRole('main');
  await expect(main.getByText('Failed', {exact: true}).first()).toBeVisible();
  await expect(main.getByText('Host Git refuses a work tree that may hold a nested')).toBeVisible();
  await expect(main.getByText('NestedRepositoryError')).toHaveCount(0);
  await expect(main.getByTitle(fixture.end?.reason ?? '', {exact: true}).first()).toBeAttached();
  await expect(main.getByRole('button', {name: 'Run again', exact: true})).toBeVisible();
  await expect(main.getByRole('button', {name: 'Resume', exact: true})).toBeVisible();
});
