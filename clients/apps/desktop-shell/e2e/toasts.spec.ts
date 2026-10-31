/**
 * Toasts and compact banners in the window: a failure passed through is a toast at the top right,
 * collapsed, below the title bar and clear of the rail; a state the screen holds is one line.
 * `TOASTS_SCREENSHOTS` names a directory for the window screenshots of the app check.
 */
import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeNewRun,
} from './fake-bridge.js';
import {addInitScripts, initScript} from './init-scripts.js';

const SHOTS = process.env['TOASTS_SCREENSHOTS'];

const TITLEBAR = 40;

const SEED: FakeBridgeSeed = {
  theme: 'ink',
  chromeInset: {width: 84, height: TITLEBAR},
  appearance: {mode: 'dark', light: 'paper', dark: 'ink'},
  listings: [],
  recent: [],
  machines: [
    {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
    {host: 'ssh:host-b', label: 'host-b', checkout: '~/vibesys'},
  ],
  reports: {'ssh:host-b': {kind: 'needs-update', facts: null}},
  facts: {
    checkout: {root: '/home/me/vibesys', version: '0.1.1', commit: 'e66d941', dirty: false},
    hardware: {cpu_cores: 64, gpus: [], accelerator: 'cpu'},
    docker: {reachable: true, version: '29.1'},
    harness_clis: ['claude'],
    harness_cli_names: {claude: 'Claude Code'},
    runs: {live: 0, ended_today: 0},
  },
};

/** The machines list the main process refused: a failure the person passes through. */
const refuseMachines = initScript(() => {
  const bridge = (window as unknown as {vibesysWelcome: Record<string, unknown>}).vibesysWelcome;
  bridge['machines'] = () =>
    Promise.resolve({ok: false, error: 'hosts.json is unreadable.', authNeeded: false});
}, undefined);

async function open(page: Page, width: number, height = 700, refused = true): Promise<void> {
  await page.setViewportSize({width, height});
  await addInitScripts(page, [
    initScript(installFakeBridge, SEED),
    initScript(installFakeMachines, SEED),
    initScript(installFakeNewRun, SEED),
    ...(refused ? [refuseMachines] : []),
  ]);
  await page.goto('./shell.html');
}

async function shoot(page: Page, name: string): Promise<void> {
  if (SHOTS !== undefined) await page.screenshot({path: `${SHOTS}/${name}.png`});
}

const toast = (page: Page) => page.getByRole('alert');

for (const width of [1440, 660]) {
  test(`a failure passed through is a small collapsed toast at the top right at ${width}px`, async ({
    page,
  }) => {
    await open(page, width);
    await expect(toast(page)).toHaveCount(1);
    await expect(toast(page)).toContainText('The machines could not be read');
    await expect(page.getByText('hosts.json is unreadable.')).toHaveCount(0);
    const box = await toast(page).boundingBox();
    const rail = await page.getByRole('navigation', {name: 'VibeSys'}).boundingBox();
    if (box === null || rail === null) throw new Error('the toast and the rail are drawn');
    // At the top right, below the title bar, inside the window, and clear of the rail.
    expect(box.y).toBeGreaterThanOrEqual(TITLEBAR);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(box.x + box.width).toBeGreaterThan(width - 40);
    expect(box.x).toBeGreaterThanOrEqual(rail.x + rail.width);
    expect(box.height).toBeLessThan(80);
    await shoot(page, `toast-collapsed-${width}`);
    await page.getByRole('button', {name: 'Show details'}).click();
    await expect(page.getByText('hosts.json is unreadable.')).toBeVisible();
    await shoot(page, `toast-open-${width}`);
    const axe = await new AxeBuilder({page}).analyze();
    expect(axe.violations.map(violation => `${violation.id}: ${violation.help}`)).toEqual([]);
    await page.getByRole('button', {name: 'Dismiss'}).click();
    await expect(toast(page)).toHaveCount(0);
  });
}

test('the toast is reachable and operable from the keyboard', async ({page}) => {
  await open(page, 1440);
  const expand = page.getByRole('button', {name: 'Show details'});
  await expect(expand).toBeVisible();
  await expand.focus();
  await expect(expand).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByText('hosts.json is unreadable.')).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', {name: 'Dismiss'})).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(toast(page)).toHaveCount(0);
});

for (const width of [1440, 660]) {
  test(`a state the screen holds is one line with its detail behind a button at ${width}px`, async ({
    page,
  }) => {
    await open(page, 1440, 900, false);
    await page
      .getByRole('navigation', {name: 'VibeSys'})
      .getByRole('button', {name: 'Settings'})
      .click();
    await page
      .getByRole('navigation', {name: 'Settings sections'})
      .getByRole('button', {name: /Machines/})
      .click();
    await page.getByRole('treeitem', {name: 'host-b'}).click();
    await page.setViewportSize({width, height: 900});
    const banner = page.locator('.vs-banner').filter({hasText: 'Needs an update'});
    await expect(banner).toBeVisible();
    const box = await banner.boundingBox();
    if (box === null) throw new Error('the banner is drawn');
    expect(box.height).toBeLessThan(56);
    await expect(page.getByText('VibeSys on host-b needs an update')).toHaveCount(0);
    await shoot(page, `banner-collapsed-${width}`);
    await banner.getByRole('button', {name: 'Show details'}).click();
    await expect(page.getByText('VibeSys on host-b needs an update')).toBeVisible();
    await shoot(page, `banner-open-${width}`);
    await expect(toast(page)).toHaveCount(0);
  });
}
