import {fileURLToPath} from 'node:url';
import {expect, type Locator, test} from '@playwright/test';

const LOCAL_INSTANCE = '0123456789ab';
const SSH_INSTANCE = 'ba9876543210';

interface InspectionCall {
  readonly host: string;
  readonly instance: string;
  readonly includeLog: boolean;
  readonly inspectionToken: number;
}

interface RendererTestControl {
  readonly inspectionCalls: InspectionCall[];
  overviewCalls: number;
  emitChrome(state: unknown): void;
  emitRecent(runs: unknown): void;
}

declare global {
  interface Window {
    desktopInspectorTest: RendererTestControl;
  }
}

const BRIDGE_FIXTURE = fileURLToPath(new URL('./fake-welcome-bridge.js', import.meta.url));

test.beforeEach(async ({context, page}) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], {
    origin: 'http://127.0.0.1:5184',
  });
  await page.addInitScript({path: BRIDGE_FIXTURE});
  await page.goto('/welcome.html');
  await expect(page.getByRole('heading', {name: 'Recent'})).toBeVisible();
});

function field(dialog: Locator, label: string): Locator {
  return dialog
    .locator('dt')
    .filter({hasText: new RegExp(`^${label}$`)})
    .locator('xpath=following-sibling::dd[1]');
}

test('local inspection loads diagnostics explicitly, copies values, and restores focus after redraw', async ({
  page,
}) => {
  const row = page.locator('#recent-section tr').filter({hasText: 'This Mac'});
  await row.getByRole('button', {name: 'Inspect'}).click();
  const dialog = page.getByRole('dialog', {name: 'Inspect local-run'});

  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button', {name: 'Close'})).toBeFocused();
  await expect(field(dialog, 'Host').locator('code')).toHaveText('This Mac');
  await expect(field(dialog, 'Hostname').locator('code')).toHaveText('workstation.local');
  await expect(dialog).toContainText('Diagnostics are not loaded until you request them.');
  await expect
    .poll(() => page.evaluate(() => window.desktopInspectorTest.inspectionCalls))
    .toEqual([{host: 'local', instance: LOCAL_INSTANCE, includeLog: false, inspectionToken: 1}]);

  await dialog.getByRole('button', {name: 'Load diagnostics'}).click();
  await expect(dialog.locator('pre')).toHaveText('workstation.local: retained diagnostics');
  await expect
    .poll(() => page.evaluate(() => window.desktopInspectorTest.inspectionCalls.at(-1)))
    .toEqual({host: 'local', instance: LOCAL_INSTANCE, includeLog: true, inspectionToken: 2});

  await field(dialog, 'Project').getByRole('button', {name: 'Copy'}).click();
  await expect(dialog.getByRole('status')).toHaveText('Project copied.');
  await expect
    .poll(() => page.evaluate(() => navigator.clipboard.readText()))
    .toBe('/projects/local');

  await dialog.getByRole('button', {name: 'Refresh'}).click();
  await expect
    .poll(() => page.evaluate(() => window.desktopInspectorTest.inspectionCalls.length))
    .toBe(3);
  expect(await page.evaluate(() => window.desktopInspectorTest.inspectionCalls.at(-1))).toEqual({
    host: 'local',
    instance: LOCAL_INSTANCE,
    includeLog: true,
    inspectionToken: 4,
  });

  await page.evaluate(
    instance =>
      window.desktopInspectorTest.emitRecent([
        {
          host: 'ssh:gpu',
          hostLabel: 'gpu',
          project: '/projects/remote',
          task: 'train',
          instanceId: instance,
          runId: 'remote-run',
          attachedAt: 1,
        },
      ]),
    SSH_INSTANCE,
  );
  await expect(row).toBeHidden();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(page.locator('#recent-section')).toBeFocused();
});

test('SSH inspection stays usable at narrow widths and follows the active theme', async ({
  page,
}) => {
  await page.setViewportSize({width: 480, height: 720});
  const lightBackground = await page
    .locator('body')
    .evaluate(node => getComputedStyle(node).backgroundColor);
  await page.emulateMedia({colorScheme: 'dark'});
  await expect
    .poll(() => page.locator('body').evaluate(node => getComputedStyle(node).backgroundColor))
    .not.toBe(lightBackground);

  const row = page.locator('#recent-section tr').filter({hasText: 'gpu'});
  const inspect = row.getByRole('button', {name: 'Inspect'});
  await inspect.click();
  const dialog = page.getByRole('dialog', {name: 'Inspect remote-run'});
  await expect(field(dialog, 'Host').locator('code')).toHaveText('gpu');
  await expect(field(dialog, 'Hostname').locator('code')).toHaveText('gpu.internal');
  await expect
    .poll(() => page.evaluate(() => window.desktopInspectorTest.inspectionCalls.at(-1)))
    .toEqual({host: 'ssh:gpu', instance: SSH_INSTANCE, includeLog: false, inspectionToken: 1});

  const columns = await dialog
    .locator('.inspection')
    .evaluate(node => getComputedStyle(node).gridTemplateColumns.trim().split(/\s+/));
  expect(columns).toHaveLength(1);
  const card = await dialog.boundingBox();
  expect(card).not.toBeNull();
  expect(card?.width ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(464);

  await dialog.getByRole('button', {name: 'Close'}).press('Shift+Tab');
  await expect(dialog.locator(':focus')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(inspect).toBeFocused();
});
