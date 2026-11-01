/**
 * The layout of the recorded-fact screens, app window only: Pause with the word "scripted" on a
 * scripted turn at the run level, and the checkout skip on an adoption step at the idea level.
 * What each says, and that a run whose log records no root is still addressed by the catalog, is
 * `src/recorded-facts.dom.test.tsx`. `SCREENS_DIR` writes each shot.
 */
import {mkdirSync} from 'node:fs';
import {join} from 'node:path';
import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed, type StoreRunFixture, storeRunFixture} from '@vibesys/run-fixtures';
import {withFacts} from '../src/testing/recorded-facts.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

const shots = process.env['SCREENS_DIR'];

async function openGateway(page: Page, fixture: StoreRunFixture): Promise<void> {
  await page.setViewportSize({width: 1440, height: 900});
  await installGateway(page, wire(fixture));
  await openFixture(page, {...fixtureNamed('queue-rs-live'), runId: 'run-a'});
  await expect(page.getByRole('heading', {name: 'queue-rs · mpmc'})).toBeVisible();
  await settled(page);
}

async function shot(page: Page, name: string): Promise<void> {
  expect(await screenCheck(page)).toEqual([]);
  if (!shots) return;
  mkdirSync(shots, {recursive: true});
  await page.screenshot({path: join(shots, `${name}.png`)});
}

test('the recorded-fact screens lay out: Pause and scripted, and the checkout skip', async ({
  page,
}) => {
  await openGateway(page, withFacts(storeRunFixture('live')));
  await expect(page.getByRole('button', {name: 'Pause', exact: true})).toBeVisible();
  await expect(page.getByText('scripted')).toBeVisible();
  await shot(page, 'pause-and-scripted');
  await page.getByRole('option', {name: 'Idea 1'}).click();
  await expect(page.getByText('Operator changed checkout')).toBeVisible();
  await settled(page);
  await shot(page, 'checkout-skip');
});
