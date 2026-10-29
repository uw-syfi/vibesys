import AxeBuilder from '@axe-core/playwright';
import {expect, test} from '@playwright/test';
import {openFixture} from './fixture-world.js';
import {journeyFixture, transitionJourney} from './journey-world.js';

test('native startup logs keep their owners through running, draining and ending', async ({
  page,
}) => {
  await openFixture(page, journeyFixture('startup'));
  const startup = page.getByRole('region', {name: 'Starting'});
  await expect(startup.getByText('Starting the agent container')).toBeVisible();
  await expect(startup.getByText('[readiness] claude: binary ok')).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await page.screenshot({path: test.info().outputPath('startup.png')});
  await transitionJourney(page, 'startup', 'startup-running');
  await expect(startup).toHaveCount(0);
  await page.getByRole('radiogroup', {name: 'Show'}).getByText('Logs').click();
  const streams = page.getByRole('listbox', {name: 'Log streams'});
  for (const name of ['Startup', 'Gates', 'Tool builds', 'Run narration']) {
    await expect(streams.getByRole('option', {name}).first()).toBeVisible();
  }
  await streams.getByRole('option', {name: /candidate build · input · attempt 1/}).click();
  await expect(page.getByRole('log').getByText('Finished release profile')).toBeVisible();
  await streams
    .getByRole('option', {name: /Run narration/})
    .last()
    .click();
  await expect(page.getByRole('log').getByText('16:36:20 INFO container ready')).toBeVisible();
  await page.screenshot({path: test.info().outputPath('logs.png')});
  await transitionJourney(page, 'startup-running', 'startup-draining');
  await expect(page.getByText('Pausing', {exact: true}).first()).toBeVisible();
  await expect(page.getByRole('log').getByText('16:36:20 INFO container ready')).toBeVisible();
  await page.screenshot({path: test.info().outputPath('draining.png')});
  await transitionJourney(page, 'startup-draining', 'startup-ended');
  await expect(
    page.getByText('W6’s benchmark timed out after 300 s.', {exact: true}),
  ).toBeVisible();
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await page.screenshot({path: test.info().outputPath('ended.png')});
});
