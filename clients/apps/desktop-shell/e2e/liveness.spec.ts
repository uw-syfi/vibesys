/** A run's lock observation is transient; finished history needs no live connection. */
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {create, toBinary} from '@bufbuild/protobuf';
import {expect, type Page, test} from '@playwright/test';
import {RecordSchema, RunLiveness, WatchRunsResponseSchema} from '@vibesys/proto';
import {fixtureNamed, storeRunFixture} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

interface GatewayWindow {
  fakeGateway: {observe(bytes: number[]): void; disconnect(): void};
}
const sheets: SheetEntry[] = [];
const shots = process.env['SCREENS_DIR'];
async function capture(page: Page, name: string): Promise<void> {
  await settled(page);
  expect(await screenCheck(page)).toEqual([]);
  if (!shots) return;
  mkdirSync(shots, {recursive: true});
  const file = `${name}.png`;
  await page.screenshot({path: join(shots, file)});
  sheets.push({name, file});
}

for (const width of [1000, 1440, 1920]) {
  for (const paused of [false, true]) {
    test(`lock release while ${paused ? 'paused' : 'running'} at ${width}px`, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      const fixture = storeRunFixture('live');
      const seq = (fixture.records.at(-1)?.seq ?? 0n) + 1n;
      const records = [
        ...fixture.records,
        create(RecordSchema, {
          seq,
          epoch: 1,
          at: {seconds: 1700000100n},
          body: {
            case: 'lifecycle',
            value: {
              state: {
                case: 'running',
                value: paused ? {hold: {reason: {case: 'operator', value: {}}}} : {},
              },
            },
          },
        }),
      ];
      await installGateway(
        page,
        wire({...fixture, records, initialCount: records.length}, RunLiveness.ALIVE),
      );
      await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
      await expect(
        page.getByText(paused ? 'Paused by you' : 'Running', {exact: true}).first(),
      ).toBeVisible();
      const hint = [
        ...toBinary(
          WatchRunsResponseSchema,
          create(WatchRunsResponseSchema, {
            runId: 'run-a',
            liveness: RunLiveness.INTERRUPTED,
            head: {process: {epoch: 1}},
          }),
        ),
      ];
      await page.evaluate(
        bytes => (window as unknown as GatewayWindow).fakeGateway.observe(bytes),
        hint,
      );
      await expect(page.getByText('Interrupted', {exact: true}).first()).toBeVisible();
      await expect(page.getByRole('main').getByText('Running', {exact: true})).toHaveCount(0);
      await expect(page.getByRole('main').getByText('Failed', {exact: true}).first()).toBeVisible();
      await expect(page.getByText('Paused by you', {exact: true})).toHaveCount(0);
      await expect(page.getByText('Live', {exact: true})).toHaveCount(0);
      await expect(page.getByText('Disconnected', {exact: true})).toHaveCount(0);
      await capture(page, `interrupted-${paused ? 'paused' : 'running'}-${width}`);
    });
  }
  test(`finished history survives follower loss at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 900});
    await installGateway(page, wire(storeRunFixture('short')));
    await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
    await expect(page.getByText('Finished', {exact: true}).first()).toBeVisible();
    await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.disconnect());
    await expect(page.getByText('Finished', {exact: true}).first()).toBeVisible();
    await expect(page.getByText('Disconnected', {exact: true})).toHaveCount(0);
    await capture(page, `finished-offline-${width}`);
  });
}

test('live connection loss remains visible', async ({page}) => {
  await installGateway(page, wire(storeRunFixture('live')));
  await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
  await expect(page.getByText('Running', {exact: true}).first()).toBeVisible();
  await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.disconnect());
  await expect(page.getByText('Reconnecting', {exact: true})).toBeVisible();
});

test('liveness app-window contact sheet', async ({page}) => {
  if (!shots || sheets.length === 0) return;
  writeFileSync(join(shots, 'liveness-contact-sheet.html'), contactSheetHtml(sheets, 3));
  await page.goto(`file://${join(shots, 'liveness-contact-sheet.html')}`);
  await page.setViewportSize({width: 1080, height: 900});
  await page.screenshot({path: join(shots, 'liveness-contact-sheet.png'), fullPage: true});
});
