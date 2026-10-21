import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {expect, test} from '@playwright/test';
import {fixtureNamed, storeRunFixture} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

const sheet: SheetEntry[] = [];
const shots = process.env['SCREENS_DIR'];
for (const kind of ['short', 'live', 'blobs'] as const)
  for (const width of [1000, 1440, 1920]) {
    test(`store Run screen ${kind} at ${width}px`, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      const fixture = storeRunFixture(kind);
      await page.addInitScript(installGateway, wire(fixture));
      await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
      const toggle = page.getByRole('switch', {name: 'Run transport v2'});
      await expect(toggle).not.toBeChecked();
      expect(
        await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.calls()),
      ).toEqual([]);
      await toggle.check();
      await expect(page.getByRole('heading', {name: 'queue-rs · mpmc'})).toBeVisible();
      await expect(page.getByRole('button', {name: /Idea 1/}).first()).toBeVisible();
      const range = page.getByRole('slider');
      await expect(range).toHaveCount(1);
      const capture = async (mode: string): Promise<void> => {
        await settled(page);
        expect(await screenCheck(page)).toEqual([]);
        if (!shots) return;
        mkdirSync(shots, {recursive: true});
        const file = `store-${kind}-${width}-${mode}.png`;
        await page.screenshot({path: join(shots, file)});
        sheet.push({name: `${kind}/${width}/${mode}`, file});
      };
      await expect
        .poll(async () =>
          page.evaluate(
            () =>
              (window as unknown as GatewayWindow).fakeGateway
                .calls()
                .filter(method => method.endsWith('/GetBlobs')).length,
          ),
        )
        .toBe(kind === 'blobs' ? 2 : 1);
      await capture('ideas');
      await range.fill('2');
      await capture('scrubbed');
      const calls = await page.evaluate(() =>
        (window as unknown as GatewayWindow).fakeGateway.calls(),
      );
      expect(calls.filter(method => method.endsWith('/Skeleton'))).toHaveLength(1);
      expect(calls.filter(method => method.endsWith('/GetBlobs'))).toHaveLength(
        kind === 'blobs' ? 2 : 1,
      );
      await page
        .getByRole('button', {name: kind === 'live' ? 'Back to live' : 'Jump to end'})
        .click();
      for (const mode of ['Timeline', 'Agents', 'Usage', 'Logs']) {
        await page.getByRole('radio', {name: mode, exact: true}).click();
        if (mode === 'Logs')
          await expect(page.getByText('Build complete', {exact: true}).last()).toBeVisible();
        await capture(mode.toLowerCase());
      }
      await page.getByRole('radio', {name: 'Ideas', exact: true}).click();
      await page.getByRole('option', {name: /^Idea 1 /}).click();
      await capture('idea');
      await page
        .getByRole('listbox', {name: /steps$/})
        .getByRole('option', {name: /Implementer/i})
        .first()
        .click();
      await expect(page.getByRole('region', {name: 'Implement', exact: true})).toBeVisible();
      await expect(page.getByText('No transcript', {exact: true})).toBeVisible();
      await capture('step');
      await toggle.uncheck();
      await expect(page.getByRole('button', {name: 'Chat', exact: true})).toBeVisible();
      expect(
        await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.active()),
      ).toBe(0);
    });
  }

test('the host switch survives missing legacy history and usage retains its model and dates', async ({
  page,
}) => {
  await page.setViewportSize({width: 1440, height: 900});
  await page.addInitScript(installGateway, wire(storeRunFixture('short')));
  await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'}, {summary: 'gone'});
  await expect(page.getByText('Could not load this run', {exact: true})).toBeVisible();
  const toggle = page.getByRole('switch', {name: 'Run transport v2'});
  await expect(toggle).not.toBeChecked();
  await toggle.check();
  await page.getByRole('radio', {name: 'Usage', exact: true}).click();
  await page.getByRole('radio', {name: 'Model', exact: true}).click();
  await page.getByRole('radio', {name: 'Tokens', exact: true}).click();
  await expect(page.getByText('gpt-5-codex', {exact: true}).last()).toBeVisible();
  await expect(page.getByText(/turns have no end time/)).toHaveCount(0);
  await settled(page);
  expect(await screenCheck(page)).toEqual([]);
  if (shots) {
    const file = 'store-usage-model-dated.png';
    await page.screenshot({path: join(shots, file)});
    sheet.push({name: 'Usage by model, dated', file});
  }
  await toggle.uncheck();
  await expect(page.getByText('Could not load this run', {exact: true})).toBeVisible();
  await expect(toggle).not.toBeChecked();
  await expect(page.getByRole('button', {name: 'Pause', exact: true})).toHaveCount(0);
  await toggle.check();
  await expect(page.getByRole('button', {name: 'Pause', exact: true})).toBeVisible();
});

test('store Run app-window contact sheet', async ({page}) => {
  if (!shots || sheet.length === 0) return;
  const html = contactSheetHtml(sheet, 3);
  writeFileSync(join(shots, 'store-contact-sheet.html'), html);
  await page.goto(`file://${join(shots, 'store-contact-sheet.html')}`);
  await page.setViewportSize({width: 1080, height: 900});
  await page.screenshot({path: join(shots, 'store-contact-sheet.png'), fullPage: true});
});
interface GatewayWindow {
  fakeGateway: {calls(): string[]; active(): number; append(bytes: number[]): void};
}
