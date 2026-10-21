/** Built Run screen replay: only this test composition mounts the unmerged controls. */
import {execFileSync} from 'node:child_process';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {expect, test} from '@playwright/test';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';

interface ReplayWindow extends Window {
  relaunchFixture: {
    release(): void;
    timeout(): void;
    epochs(): number;
    cancelled(): number;
  };
}
test.beforeAll(() => {
  execFileSync('pnpm', ['exec', 'vite', 'build', '--config', 'e2e/relaunch/vite.config.ts'], {
    stdio: 'pipe',
    timeout: 30_000,
  });
});

test('Relaunch replay states and epoch boundary on the built Run screen', async ({page}) => {
  test.setTimeout(60_000);
  const shots = process.env['SCREENS_DIR'];
  const before: SheetEntry[] = [];
  const after: SheetEntry[] = [];
  const capture = async (name: string, entries = after): Promise<void> => {
    await settled(page);
    expect(await screenCheck(page)).toEqual([]);
    if (!shots) return;
    mkdirSync(shots, {recursive: true});
    const file = `relaunch-${name}.png`;
    await page.screenshot({path: join(shots, file)});
    entries.push({name, file});
  };
  for (const width of [1000, 1440, 1920]) {
    await page.setViewportSize({width, height: 900});
    await page.goto('/relaunch/fixture.html?before');
    await expect(page.getByText('Run controls', {exact: true})).toBeVisible();
    await page.getByText('Run controls', {exact: true}).scrollIntoViewIfNeeded();
    await expect(page.getByRole('button', {name: 'Relaunch', exact: true})).toHaveCount(0);
    await capture(`before-${width}`, before);
    for (const state of ['parked', 'stopped-resumable', 'interrupted']) {
      await page.goto(`/relaunch/fixture.html?state=${state}`);
      const launch = page.getByRole('button', {name: 'Relaunch', exact: true});
      await expect(launch).toBeEnabled();
      await launch.scrollIntoViewIfNeeded();
      await capture(`${state}-${width}-ready`);
      await launch.click();
      await expect(page.getByRole('button', {name: 'Relaunching'})).toBeDisabled();
      await capture(`${state}-${width}-launching`);
      await page.evaluate(() => (window as unknown as ReplayWindow).relaunchFixture.release());
      await expect(page.getByText('Epoch 2', {exact: true})).toBeVisible();
      expect(
        await page.evaluate(() => (window as unknown as ReplayWindow).relaunchFixture.epochs()),
      ).toBe(2);
      await capture(`${state}-${width}-epoch`);
    }
  }
  await page.setViewportSize({width: 1000, height: 900});
  for (const [result, label] of [
    ['locked', 'Refused: locked'],
    ['ended', 'Refused: ended'],
    ['checkout', 'Refused: checkout missing'],
    ['epoch', 'Refused: epoch changed'],
    ['timeout', 'Relaunch not confirmed. Check the timeline before trying again.'],
  ]) {
    await page.goto(`/relaunch/fixture.html?result=${result}`);
    await page.getByRole('button', {name: 'Relaunch', exact: true}).click();
    if (result === 'timeout')
      await page.evaluate(() => (window as unknown as ReplayWindow).relaunchFixture.timeout());
    else await page.evaluate(() => (window as unknown as ReplayWindow).relaunchFixture.release());
    await expect(page.getByText(label ?? '', {exact: true})).toBeVisible();
    expect(
      await page.evaluate(() => (window as unknown as ReplayWindow).relaunchFixture.cancelled()),
    ).toBe(1);
    await capture(result ?? '');
  }
  if (!shots) return;
  for (const [name, entries] of [
    ['before', before],
    ['after', after],
  ] as const) {
    writeFileSync(join(shots, `${name}-contact-sheet.html`), contactSheetHtml(entries, 3));
    await page.goto(`file://${join(shots, `${name}-contact-sheet.html`)}`);
    await page.screenshot({path: join(shots, `${name}-contact-sheet.png`), fullPage: true});
  }
});
