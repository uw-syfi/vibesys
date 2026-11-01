import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed, storeRunFixture} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

const sheet: SheetEntry[] = [];
const shots = process.env['SCREENS_DIR'];
const WIDTHS = [1000, 1440, 1920];

/** Lays the screen the page shows out at every width, on the one page: the layout is what changes. */
async function capture(page: Page, name: string, label: string): Promise<void> {
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 900});
    await settled(page);
    expect(await screenCheck(page)).toEqual([]);
    if (!shots) continue;
    mkdirSync(shots, {recursive: true});
    const file = `${name}-${width}.png`;
    await page.screenshot({path: join(shots, file)});
    sheet.push({name: `${label}/${width}`, file});
  }
}

// What the screen reads and asks of the gateway is `src/gateway-records.dom.test.tsx`; here each
// face and level of each kind of run is laid out at every width.
for (const kind of ['short', 'live', 'blobs'] as const) {
  test(`store Run screen ${kind} lays out at every width`, async ({page}) => {
    await page.setViewportSize({width: WIDTHS[0] ?? 1000, height: 900});
    await installGateway(page, wire(storeRunFixture(kind)));
    await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
    await expect(page.getByRole('button', {name: /Idea 1/}).first()).toBeVisible();
    const view = async (mode: string): Promise<void> =>
      capture(page, `store-${kind}-${mode}`, `${kind}/${mode}`);
    await view('ideas');
    await page.getByRole('slider').fill('2');
    await view('scrubbed');
    await page
      .getByRole('button', {name: kind === 'live' ? 'Back to live' : 'Jump to end'})
      .click();
    for (const mode of ['Timeline', 'Agents', 'Usage', 'Logs']) {
      await page.getByRole('radio', {name: mode, exact: true}).click();
      if (mode === 'Logs')
        await expect(page.getByText('Build complete', {exact: true}).last()).toBeVisible();
      await view(mode.toLowerCase());
    }
    await page.getByRole('radio', {name: 'Ideas', exact: true}).click();
    await page.getByRole('option', {name: /^Idea 1(?!\d)/}).click();
    await view('idea');
    await page
      .getByRole('listbox', {name: /steps$/})
      .getByRole('option', {name: /builder/i})
      .first()
      .click();
    await expect(page.getByRole('region', {name: 'Implement', exact: true})).toBeVisible();
    await view('step');
  });
}

test('the Usage face by model, tokens and dates lays out', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 900});
  await installGateway(page, wire(storeRunFixture('short')));
  await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
  await page.getByRole('radio', {name: 'Usage', exact: true}).click();
  await page.getByRole('radio', {name: 'Model', exact: true}).click();
  await page.getByRole('radio', {name: 'Tokens', exact: true}).click();
  await expect(page.getByText('model-a', {exact: true}).last()).toBeVisible();
  await settled(page);
  expect(await screenCheck(page)).toEqual([]);
  if (shots) {
    const file = 'store-usage-model-dated.png';
    await page.screenshot({path: join(shots, file)});
    sheet.push({name: 'Usage by model, dated', file});
  }
});

test('store Run app-window contact sheet', async ({page}) => {
  if (!shots || sheet.length === 0) return;
  const html = contactSheetHtml(sheet, 3);
  writeFileSync(join(shots, 'store-contact-sheet.html'), html);
  await page.goto(`file://${join(shots, 'store-contact-sheet.html')}`);
  await page.setViewportSize({width: 1080, height: 900});
  await page.screenshot({path: join(shots, 'store-contact-sheet.png'), fullPage: true});
});
