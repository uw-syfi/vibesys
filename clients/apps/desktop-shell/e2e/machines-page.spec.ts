import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {rawTextOf} from './checks/screen-check.js';
import {card, type MachinesWorld, openMachines, rail, testOf, WORLDS} from './machines-page.js';

function world(name: string): MachinesWorld {
  const found = WORLDS.find(each => each.name === name);
  if (found === undefined) throw new Error(`no world named ${name}`);
  return found;
}

test('each machine is a card with its clusters and clouds nested under it', async ({page}) => {
  await openMachines(page, 1440, world('several machines'));
  const names = (machine: string) =>
    card(page, machine)
      .getByRole('row')
      .filter({hasNot: page.getByRole('columnheader')})
      .locator('td:first-child')
      .allTextContents();
  expect(await names('This Mac')).toEqual(['lab-h100', 'campus', 'cluster-m', 'Modal']);
  expect(await names('host-a')).toEqual(['lab-h100']);
  expect(await names('host-b')).toEqual([]);
  // A machine with nothing set up shows nothing under it, not a word for nothing.
  await expect(card(page, 'host-b').getByRole('table')).toHaveCount(0);
  await expect(card(page, 'host-b').getByText('None', {exact: true})).toHaveCount(0);
  // A helper that predates compute settings lists nothing, and says so in a word.
  await expect(card(page, 'host-c').getByText('Unavailable', {exact: true})).toBeVisible();
  await expect(card(page, 'This Mac').getByText('Online', {exact: true})).toBeVisible();
  await expect(card(page, 'This Mac').getByText('64 CPU · 8× H100')).toBeVisible();
  await expect(card(page, 'This Mac').getByText('2 live', {exact: true})).toBeVisible();
  // The count is the link to the runs, and it is not there at zero.
  await expect(
    card(page, 'This Mac').getByRole('button', {name: '3 runs', exact: true}),
  ).toBeVisible();
  await expect(
    card(page, 'host-a').getByRole('button', {name: '1 run', exact: true}),
  ).toBeVisible();
  await expect(card(page, 'host-b').getByRole('button', {name: /run/})).toHaveCount(0);
  await expect(page.getByText('Show runs')).toHaveCount(0);
  // An online machine has nothing to reconnect.
  await expect(card(page, 'This Mac').getByRole('button', {name: 'Reconnect'})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Add machine'})).toBeVisible();
  expect(await rawTextOf(page)).toEqual([]);
  const axe = await new AxeBuilder({page}).analyze();
  expect(axe.violations).toEqual([]);
});

test('a GPU reads one way, on the machine and in every cluster row', async ({page}) => {
  await openMachines(page, 1440, world('several machines'));
  const text = await card(page, 'This Mac').innerText();
  expect(text).toContain('64 CPU · 8× H100');
  expect(text).toContain('8× H100');
  expect(text).toContain('8× MI300X');
  expect(text).not.toMatch(/nvidia_|× (h100|mi300x)/);
});

test('the Test column has no header word, and each Test button is centred on its row', async ({
  page,
}) => {
  await openMachines(page, 1440, world('several machines'));
  const mac = card(page, 'This Mac');
  const header = mac.getByRole('columnheader', {name: 'Test'}).locator('span');
  expect((await header.boundingBox())?.width).toBeLessThanOrEqual(1);
  const name = mac
    .getByRole('row', {name: /lab-h100/})
    .getByRole('cell')
    .first();
  const button = testOf(page, 'This Mac', 'lab-h100');
  const buttonBox = await button.boundingBox();
  const middle = (box: {y: number; height: number} | null) =>
    (box?.y ?? 0) + (box?.height ?? 0) / 2;
  // The button's middle is the row text's middle, to within a pixel.
  const text = await name.evaluate(el => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const rect = range.getBoundingClientRect();
    return rect.y + rect.height / 2;
  });
  expect(Math.abs(middle(buttonBox) - text)).toBeLessThanOrEqual(1);
});

test('Test says when a cluster was checked, or since when it is unreachable', async ({page}) => {
  await openMachines(page, 1440, world('an unreachable cluster'));
  const row = (cluster: string) =>
    card(page, 'This Mac').getByRole('row', {name: new RegExp(cluster)});
  // The world's test of standby found it unreachable.
  await expect(row('standby')).toContainText(/Unreachable since \d\d:\d\d/);
  await expect(row('lab-h100')).toContainText('Not checked');
  await testOf(page, 'This Mac', 'lab-h100').click();
  await expect(row('lab-h100')).toContainText(/Checked \d\d:\d\d/);
  await expect(row('Modal')).toContainText('Token found');
  await expect(row('Modal').getByRole('button')).toHaveCount(0);
});

test('a test under way says so until it answers', async ({page}) => {
  await openMachines(page, 1000, world('a test in progress'));
  await expect(card(page, 'This Mac').getByRole('row', {name: /lab-h100/})).toContainText(
    'Testing',
  );
  // Another cluster can be tested meanwhile; this one cannot be pressed again.
  await expect(testOf(page, 'This Mac', 'campus')).toHaveText('Test');
  await page.evaluate(() => (window as unknown as {finishTests: () => void}).finishTests());
  await expect(testOf(page, 'This Mac', 'lab-h100')).toHaveText('Test');
  await expect(card(page, 'This Mac').getByRole('row', {name: /lab-h100/})).toContainText(
    /Checked \d\d:\d\d/,
  );
});

test('an unreachable machine offers Reconnect, one to sign in offers Sign in, and neither lists anything', async ({
  page,
}) => {
  await openMachines(page, 1440, world('an unreachable machine'));
  const down = card(page, 'host-a');
  await expect(down.getByText('Unreachable', {exact: true})).toBeVisible();
  await expect(down.getByRole('row')).toHaveCount(0);
  await expect(down.getByRole('button', {name: 'Reconnect'})).toBeVisible();
  // Nothing is known of its hardware: no placeholder dash stands in for it.
  await expect(down.getByText(/^[–-]$/)).toHaveCount(0);
  const waiting = card(page, 'host-b');
  await expect(waiting.getByText('Sign in', {exact: true}).first()).toBeVisible();
  await expect(waiting.getByRole('button', {name: 'Reconnect'})).toHaveCount(0);
  await waiting.getByRole('button', {name: 'Sign in'}).click();
  await expect(waiting.getByText('Online', {exact: true})).toBeVisible();
  await expect(waiting.getByRole('button', {name: 'Sign in'})).toHaveCount(0);
  await expect(waiting.getByText('None', {exact: true})).toHaveCount(0);
});

test('the run count narrows the dashboard to that machine, and its chip removes the filter', async ({
  page,
}) => {
  await openMachines(page, 1440, world('several machines'));
  await card(page, 'host-c').getByRole('button', {name: '12 runs'}).click();
  await expect(page.getByRole('heading', {level: 1, name: 'Runs'})).toBeVisible();
  // Twelve runs on host-c, and none of the other machines' runs.
  await expect(page.locator('.vs-overview-tile')).toHaveCount(12);
  await page.getByRole('button', {name: 'Remove host-c'}).click();
  await expect(page.getByRole('button', {name: 'Remove host-c'})).toHaveCount(0);
  // 3 on This Mac, 1 on host-a and 12 on host-c.
  await expect(page.locator('.vs-overview-tile')).toHaveCount(16);
});

test("a machine's Settings opens on its clusters, and Back returns to Machines", async ({page}) => {
  await openMachines(page, 1440, world('several machines'));
  await card(page, 'host-a').getByRole('button', {name: 'Settings'}).click();
  await expect(
    page.getByRole('navigation', {name: 'Settings sections'}).getByRole('button', {
      name: 'Clusters and clouds',
    }),
  ).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('combobox', {name: 'Machine'})).toHaveValue('ssh:host-a');
  await page.getByRole('button', {name: 'Back', exact: true}).click();
  await expect(page.getByRole('heading', {level: 1, name: 'Machines'})).toBeVisible();
});

/** The rail's Machines button carries its warning in its name: the word the machine's card uses. */
const warning = (page: Page, word = 'Unreachable') =>
  rail(page).getByRole('button', {name: new RegExp(`^Machines, ${word}$`)});

test('the rail warns of nothing while every machine and cluster is fine', async ({page}) => {
  await openMachines(page, 1440, world('one machine'));
  await expect(warning(page)).toHaveCount(0);
});

test('the rail warns while a machine is unreachable, and signing one in clears only its own', async ({
  page,
}) => {
  await openMachines(page, 1440, world('an unreachable machine'));
  await expect(warning(page)).toBeVisible();
  await card(page, 'host-b').getByRole('button', {name: 'Sign in'}).click();
  await expect(card(page, 'host-b').getByText('Online', {exact: true})).toBeVisible();
  // host-a still does not answer.
  await expect(warning(page)).toBeVisible();
});

test('the rail says Needs an update, not unreachable, for a machine whose VibeSys is too old', async ({
  page,
}) => {
  const one = world('one machine');
  const old: MachinesWorld = {
    ...one,
    seed: {...one.seed, reports: {local: {kind: 'needs-update', facts: null}}},
  };
  await openMachines(page, 1440, old);
  await expect(card(page, 'This Mac').getByText('Needs an update', {exact: true})).toBeVisible();
  await expect(warning(page, 'Needs an update')).toBeVisible();
  await expect(warning(page)).toHaveCount(0);
});

test('the rail names the most urgent of the problems machines have', async ({page}) => {
  const several = world('several machines');
  const mixed: MachinesWorld = {
    ...several,
    seed: {
      ...several.seed,
      reports: {
        local: {kind: 'needs-update', facts: null},
        'ssh:host-a': {kind: 'sign-in', detail: 'passphrase'},
      },
    },
  };
  await openMachines(page, 1440, mixed);
  await expect(warning(page, 'Sign in')).toBeVisible();
});

test('the rail warns when a cluster test finds it unreachable', async ({page}) => {
  await openMachines(page, 1440, world('an unreachable cluster'));
  await expect(warning(page)).toBeVisible();
});

test('Add machine opens its form', async ({page}) => {
  await openMachines(page, 1440, world('several machines'));
  await page.getByRole('button', {name: 'Add machine'}).click();
  await expect(page.getByRole('textbox', {name: 'SSH host'})).toBeFocused();
});
