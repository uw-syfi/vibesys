/**
 * The Machines page over the worlds of the browser journeys: each machine a card with its clusters
 * and clouds, what a test says, a machine that cannot be reached or signed in, the run count that
 * narrows the dashboard, and the rail's warning. The hosts' live data comes through the same
 * scripted port (`testing/subscription-preload.ts`) the browser serves. Drawn in a DOM; these held
 * no layout, so they ran in Chromium for nothing. The browser keeps where things sit and axe.
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {press} from './testing/dom-controls.js';
import {
  all,
  drawnTexts,
  maybe,
  notSee,
  one,
  see,
  settle,
  textOf,
  until,
} from './testing/dom-query.js';
import {type MachinesWorld, subscriptionSeedOf, WORLDS} from './testing/machines-world.js';
import {rawTextIn} from './testing/raw-text.js';
import {RAW_TEXT_INPUT} from './testing/raw-text-input.js';
import {type OpenShell, openShell} from './testing/shell-world.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

const shells: OpenShell[] = [];
afterEach(async () => {
  for (const shell of shells.splice(0)) await shell.close();
});

const NOW = Date.parse('2026-10-17T12:00:00Z');

function world(name: string): MachinesWorld {
  const found = WORLDS.find(each => each.name === name);
  if (found === undefined) throw new Error(`no world named ${name}`);
  return found;
}

/** The cluster tests answer when `finishTests()` is called, so a test can be looked at under way. */
function holdTests(bridge: Record<string, unknown>): void {
  const answer = bridge['testCluster'] as (...args: unknown[]) => Promise<unknown>;
  const waiting: (() => void)[] = [];
  bridge['testCluster'] = (...args: unknown[]) =>
    new Promise(resolve => waiting.push(() => resolve(answer(...args))));
  Object.assign(globalThis.window, {
    finishTests: () => {
      for (const release of waiting.splice(0)) release();
    },
  });
}

const cardOf = (shell: OpenShell, machine: string): HTMLElement => {
  const found = [...shell.container.querySelectorAll<HTMLElement>('div.vs-card')].filter(
    each => maybe(each, {role: 'heading', name: machine, exact: true}) !== null,
  );
  if (found.length !== 1) throw new Error(`${found.length} cards for ${machine}`);
  return found[0] as HTMLElement;
};

const rowsOf = (scope: Element): Element[] =>
  all(scope, {role: 'row'}).filter(row => all(row, {role: 'columnheader'}).length === 0);

const testOf = (shell: OpenShell, machine: string, cluster: string): HTMLElement => {
  const row = all(cardOf(shell, machine), {role: 'row', name: new RegExp(cluster)})[0];
  if (row === undefined) throw new Error(`no row for ${cluster}`);
  return one(row, {role: 'button'});
};

const rowFor = (shell: OpenShell, machine: string, cluster: string): Element => {
  const row = all(cardOf(shell, machine), {role: 'row', name: new RegExp(cluster)})[0];
  if (row === undefined) throw new Error(`no row for ${cluster}`);
  return row;
};

const rail = (shell: OpenShell): HTMLElement =>
  one(shell.container, {role: 'navigation', name: 'VibeSys'});

/** The rail's Machines button carries its warning in its name: the word the machine's card uses. */
const warning = (shell: OpenShell, word = 'Unreachable'): HTMLElement | null =>
  maybe(rail(shell), {role: 'button', name: new RegExp(`^Machines, ${word}$`)});

/** The page over `scripted`, once every machine's first answer is in. */
async function open(scripted: MachinesWorld): Promise<OpenShell> {
  const shell = await openShell(
    {kind: 'machines'},
    {
      schedule: new FakeClock().schedule,
      seed: scripted.seed,
      subscriptions: subscriptionSeedOf(scripted),
      now: NOW,
      ...(scripted.holdTests === true ? {prepare: holdTests} : {}),
    },
  );
  shells.push(shell);
  one(shell.container, {role: 'heading', level: 1, name: 'Machines'});
  const first = scripted.seed.machines?.[0]?.label ?? '';
  await until(`the card of ${first}`, () =>
    [...shell.container.querySelectorAll('div.vs-card')].some(each => textOf(each).includes(first)),
  );
  // Every machine's first answer is in before the page is looked at.
  await until('the checks to finish', () => !textOf(shell.container).includes('Checking'));
  if (scripted.testFirst !== undefined) {
    const {machine, cluster, shows} = scripted.testFirst;
    await until(
      `the row of ${cluster}`,
      () => all(cardOf(shell, machine), {role: 'row', name: new RegExp(cluster)}).length > 0,
    );
    await press(testOf(shell, machine, cluster));
    await settle();
    if (shows === 'Testing…') await see(testOf(shell, machine, cluster), shows, true);
    else await see(shell.container, shows);
  }
  return shell;
}

test('each machine is a card with its clusters and clouds nested under it', async () => {
  const shell = await open(world('several machines'));
  const names = (machine: string): string[] =>
    rowsOf(cardOf(shell, machine)).map(row => textOf(row.querySelector('td:first-child') ?? row));
  expect(names('This Mac')).toEqual(['lab-h100', 'campus', 'cluster-m', 'Modal']);
  expect(names('host-a')).toEqual(['lab-h100']);
  expect(names('host-b')).toEqual([]);
  // A machine with nothing set up shows nothing under it, not a word for nothing.
  expect(all(cardOf(shell, 'host-b'), {role: 'table'})).toHaveLength(0);
  notSee(cardOf(shell, 'host-b'), 'None', true);
  // A helper that predates compute settings lists nothing, and says so in a word.
  await see(cardOf(shell, 'host-c'), 'Unavailable', true);
  await see(cardOf(shell, 'This Mac'), 'Online', true);
  await see(cardOf(shell, 'This Mac'), '64 CPU · 8× H100');
  await see(cardOf(shell, 'This Mac'), '2 live', true);
  // The count is the link to the runs, and it is not there at zero.
  one(cardOf(shell, 'This Mac'), {role: 'button', name: '3 runs', exact: true});
  one(cardOf(shell, 'host-a'), {role: 'button', name: '1 run', exact: true});
  expect(all(cardOf(shell, 'host-b'), {role: 'button', name: /run/})).toHaveLength(0);
  notSee(shell.container, 'Show runs');
  // An online machine has nothing to reconnect.
  expect(all(cardOf(shell, 'This Mac'), {role: 'button', name: 'Reconnect'})).toHaveLength(0);
  one(shell.container, {role: 'button', name: 'Add machine'});
  expect(rawTextIn(drawnTexts(shell.container), RAW_TEXT_INPUT)).toEqual([]);
});

test('a GPU reads one way, on the machine and in every cluster row', async () => {
  const shell = await open(world('several machines'));
  const text = textOf(cardOf(shell, 'This Mac'));
  expect(text).toContain('64 CPU · 8× H100');
  expect(text).toContain('8× H100');
  expect(text).toContain('8× MI300X');
  expect(text).not.toMatch(/nvidia_|× (h100|mi300x)/);
});

test('Test says when a cluster was checked, or since when it is unreachable', async () => {
  const shell = await open(world('an unreachable cluster'));
  // The world's test of standby found it unreachable.
  expect(textOf(rowFor(shell, 'This Mac', 'standby'))).toMatch(/Unreachable since \d\d:\d\d/);
  expect(textOf(rowFor(shell, 'This Mac', 'lab-h100'))).toContain('Not checked');
  await press(testOf(shell, 'This Mac', 'lab-h100'));
  await until('lab-h100 checked', () =>
    /Checked \d\d:\d\d/.test(textOf(rowFor(shell, 'This Mac', 'lab-h100'))),
  );
  expect(textOf(rowFor(shell, 'This Mac', 'Modal'))).toContain('Token found');
  expect(all(rowFor(shell, 'This Mac', 'Modal'), {role: 'button'})).toHaveLength(0);
});

test('a test under way says so until it answers', async () => {
  const shell = await open(world('a test in progress'));
  expect(textOf(rowFor(shell, 'This Mac', 'lab-h100'))).toContain('Testing');
  // Another cluster can be tested meanwhile; this one cannot be pressed again.
  expect(textOf(testOf(shell, 'This Mac', 'campus'))).toBe('Test');
  await act(async () => (globalThis.window as unknown as {finishTests: () => void}).finishTests());
  await until('the test to answer', () => textOf(testOf(shell, 'This Mac', 'lab-h100')) === 'Test');
  expect(textOf(rowFor(shell, 'This Mac', 'lab-h100'))).toMatch(/Checked \d\d:\d\d/);
});

test('an unreachable machine offers Reconnect, one to sign in offers Sign in, and neither lists anything', async () => {
  const shell = await open(world('an unreachable machine'));
  const down = cardOf(shell, 'host-a');
  await see(down, 'Unreachable', true);
  expect(all(down, {role: 'row'})).toHaveLength(0);
  one(down, {role: 'button', name: 'Reconnect'});
  // Nothing is known of its hardware: no placeholder dash stands in for it.
  expect([...down.querySelectorAll('*')].filter(each => /^[–-]$/.test(textOf(each)))).toHaveLength(
    0,
  );
  const waiting = cardOf(shell, 'host-b');
  await see(waiting, 'Sign in', true);
  expect(all(waiting, {role: 'button', name: 'Reconnect'})).toHaveLength(0);
  await press(one(waiting, {role: 'button', name: 'Sign in'}));
  await until('host-b online', () => textOf(cardOf(shell, 'host-b')).includes('Online'));
  expect(all(cardOf(shell, 'host-b'), {role: 'button', name: 'Sign in'})).toHaveLength(0);
  notSee(cardOf(shell, 'host-b'), 'None', true);
});

test('the run count narrows the dashboard to that machine, and its chip removes the filter', async () => {
  const shell = await open(world('several machines'));
  await press(one(cardOf(shell, 'host-c'), {role: 'button', name: '12 runs'}));
  await shell.read();
  one(shell.container, {role: 'heading', level: 1, name: 'Runs'});
  // Twelve runs on host-c, and none of the other machines' runs.
  expect(shell.container.querySelectorAll('.vs-overview-tile')).toHaveLength(12);
  await press(one(shell.container, {role: 'button', name: 'Remove host-c'}));
  await settle();
  await shell.read();
  expect(all(shell.container, {role: 'button', name: 'Remove host-c'})).toHaveLength(0);
  // 3 on This Mac, 1 on host-a and 12 on host-c.
  expect(shell.container.querySelectorAll('.vs-overview-tile')).toHaveLength(16);
});

test("a machine's Settings opens on its clusters, and Back returns to Machines", async () => {
  const shell = await open(world('several machines'));
  await press(one(cardOf(shell, 'host-a'), {role: 'button', name: 'Settings'}));
  await shell.read();
  const sections = one(shell.container, {role: 'navigation', name: 'Settings sections'});
  const clusters = all(sections, {role: 'button', name: 'Clusters and clouds'}).filter(
    each => !each.hasAttribute('aria-controls'),
  );
  expect(clusters).toHaveLength(1);
  expect(clusters[0]?.getAttribute('aria-current')).toBe('page');
  expect(
    (one(shell.container, {role: 'combobox', name: 'Machine'}) as HTMLSelectElement).value,
  ).toBe('ssh:host-a');
  await press(one(shell.container, {role: 'button', name: 'Back', exact: true}));
  await shell.read();
  one(shell.container, {role: 'heading', level: 1, name: 'Machines'});
});

test('the rail warns of nothing while every machine and cluster is fine', async () => {
  const shell = await open(world('one machine'));
  expect(warning(shell)).toBeNull();
});

test('the rail warns while a machine is unreachable, and signing one in clears only its own', async () => {
  const shell = await open(world('an unreachable machine'));
  await until('the warning', () => warning(shell) !== null);
  await press(one(cardOf(shell, 'host-b'), {role: 'button', name: 'Sign in'}));
  await until('host-b online', () => textOf(cardOf(shell, 'host-b')).includes('Online'));
  // host-a still does not answer.
  expect(warning(shell)).not.toBeNull();
});

test('the rail says Needs an update, not unreachable, for a machine whose VibeSys is too old', async () => {
  const one_ = world('one machine');
  const old: MachinesWorld = {
    ...one_,
    seed: {...one_.seed, reports: {local: {kind: 'needs-update', facts: null}}},
  };
  const shell = await open(old);
  await see(cardOf(shell, 'This Mac'), 'Needs an update', true);
  await until('the rail to say so', () => warning(shell, 'Needs an update') !== null);
  expect(warning(shell)).toBeNull();
});

test('the rail names the most urgent of the problems machines have', async () => {
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
  const shell = await open(mixed);
  await until('the rail to say Sign in', () => warning(shell, 'Sign in') !== null);
});

test('the rail warns when a cluster test finds it unreachable', async () => {
  const shell = await open(world('an unreachable cluster'));
  await until('the warning', () => warning(shell) !== null);
});

test('Add machine opens its form', async () => {
  const shell = await open(world('several machines'));
  await press(one(shell.container, {role: 'button', name: 'Add machine'}));
  await settle();
  const host = one(shell.container, {role: 'textbox', name: 'SSH host'});
  expect(host.ownerDocument.activeElement).toBe(host);
});
