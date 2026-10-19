import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {openFixture} from './fixture-world.js';

/**
 * A real run that crashed after its implementer turn finished, as the Run screen shows it: nothing
 * it left open reads as running, the turn and the idea last as long as they did, the baseline's
 * failed gates are listed with their reason (what they printed behind a disclosure), and the
 * banner gives the whole failure. `RUN_SHOTS` names a directory to save the app window in.
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

test('the failed baseline gates are listed with their reason, and what they printed is behind a disclosure', async ({
  page,
}) => {
  await openFixture(page, fixtureNamed('crashed-after-turn'));
  const gates = page.locator('.vs-check-list');
  await expect(gates.getByText('Accuracy · Benchmark', {exact: true})).toBeVisible();
  await expect(
    gates.getByText("ModuleNotFoundError: No module named 'engine'", {exact: true}),
  ).toHaveCount(1);
  const first = gates.locator('details').first();
  await expect(first.locator('pre')).toBeHidden();
  await first.locator('summary').click();
  await expect(first.locator('pre')).toContainText('Benchmark');
  await shot(page, 'gates');
});

test('the banner gives the whole reason the run failed', async ({page}) => {
  const fixture = fixtureNamed('crashed-after-turn');
  await openFixture(page, fixture);
  await expect(page.getByRole('main').getByText(fixture.end?.reason ?? '')).toBeVisible();
});
