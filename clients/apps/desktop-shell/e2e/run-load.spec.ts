import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed, storeRunFixture} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {type GatewayBehavior, installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

const sheet: SheetEntry[] = [];
const shots = process.env['SCREENS_DIR'];
const NOT_FOUND = 5;
const UNAVAILABLE = 14;

async function capture(page: Page, name: string, file: string): Promise<void> {
  await settled(page);
  expect(await screenCheck(page)).toEqual([]);
  if (!shots) return;
  mkdirSync(shots, {recursive: true});
  await page.screenshot({path: join(shots, file)});
  sheet.push({name, file});
}

async function openRun(page: Page, behavior: GatewayBehavior, initialCount?: number) {
  await page.setViewportSize({width: 1440, height: 900});
  const fixture = storeRunFixture('short', 12);
  await installGateway(page, {
    ...wire({...fixture, initialCount: initialCount ?? fixture.records.length}),
    behavior,
  });
  await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
}

test('a fast load paints the run and shows no bar', async ({page}) => {
  await openRun(page, {});
  await expect(page.getByRole('heading', {name: 'queue-rs · mpmc'})).toBeVisible();
  await expect(page.getByRole('img', {name: 'History'})).toHaveCount(0);
  await capture(page, 'fast load', 'load-fast.png');
});

test('a slow load paints from the first reply and shows the history filling in', async ({page}) => {
  await openRun(page, {holdTail: true}, 6);
  await expect(page.getByRole('heading', {name: 'queue-rs · mpmc'})).toBeVisible();
  await expect(page.getByText(/^History \d+ of \d+$/)).toBeVisible();
  await capture(page, 'slow load, mid-history', 'load-slow.png');
  await page.evaluate(() =>
    (window as unknown as {fakeGateway: {releaseTail(): void}}).fakeGateway.releaseTail(),
  );
  await expect(page.getByText(/^History \d+ of \d+$/)).toHaveCount(0);
});

test('a run with no log is refused in words, with Retry and no retry storm', async ({page}) => {
  await openRun(page, {skeleton: {status: NOT_FOUND, message: 'run.log is missing'}});
  await expect(page.getByText('No run log', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Retry', exact: true})).toBeVisible();
  await capture(page, 'not found', 'load-not-found.png');
  const skeletons = () =>
    page.evaluate(
      () =>
        (window as unknown as {fakeGateway: {calls(): string[]}}).fakeGateway
          .calls()
          .filter(method => method.endsWith('/Skeleton')).length,
    );
  expect(await skeletons()).toBe(1);
  await page.getByRole('button', {name: 'Retry', exact: true}).click();
  await expect.poll(skeletons).toBe(2);
});

test.describe('with the clipboard', () => {
  test.use({permissions: ['clipboard-read', 'clipboard-write']});
  test('a run with no log copies its details', async ({page}) => {
    await openRun(page, {skeleton: {status: NOT_FOUND, message: 'run.log is missing'}});
    const copy = page.getByRole('button', {name: 'Copy details', exact: true});
    await copy.click();
    await expect(page.getByRole('button', {name: 'Copied', exact: true})).toBeVisible();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    expect(copied.startsWith('No run log')).toBe(true);
    expect(copied).toContain('run.log is missing');
  });
});

test('a transient failure shows reconnecting, backs off, and ends in a status word', async ({
  page,
}) => {
  await page.clock.install();
  await openRun(page, {skeleton: {status: UNAVAILABLE, message: 'unavailable'}});
  await expect(page.getByText('Reconnecting', {exact: true}).first()).toBeVisible();
  await capture(page, 'transient failure, retrying', 'load-reconnecting.png');
  // Each retry is armed after its failure settles, so time moves in steps of the backoff cap.
  for (let step = 0; step < 8; step += 1) await page.clock.fastForward(31_000);
  await expect(page.getByText('Unreachable', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Retry', exact: true})).toBeVisible();
  await capture(page, 'transient failure, gave up', 'load-unreachable.png');
});

test('run load app-window contact sheet', async ({page}) => {
  if (!shots || sheet.length === 0) return;
  writeFileSync(join(shots, 'load-contact-sheet.html'), contactSheetHtml(sheet, 3));
  await page.goto(`file://${join(shots, 'load-contact-sheet.html')}`);
  await page.setViewportSize({width: 1080, height: 900});
  await page.screenshot({path: join(shots, 'load-contact-sheet.png'), fullPage: true});
});
