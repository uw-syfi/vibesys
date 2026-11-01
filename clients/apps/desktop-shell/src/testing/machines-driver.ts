/**
 * Driving the Machines page drawn in a DOM: a card found by its machine, a cluster's row and its
 * Test, the rail's warning, and the page opened over a world with its hosts' live data scripted.
 * The caller closes the shell it is given.
 */
import {FakeClock} from '@vibesys/transport/testing';
import {press} from './dom-controls.js';
import {all, maybe, one, see, settle, textOf, until} from './dom-query.js';
import {type MachinesWorld, subscriptionSeedOf, WORLDS} from './machines-world.js';
import {type OpenShell, openShell} from './shell-world.js';

const NOW = Date.parse('2026-10-17T12:00:00Z');

export function world(name: string): MachinesWorld {
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

export const cardOf = (shell: OpenShell, machine: string): HTMLElement => {
  const found = [...shell.container.querySelectorAll<HTMLElement>('div.vs-card')].filter(
    each => maybe(each, {role: 'heading', name: machine, exact: true}) !== null,
  );
  if (found.length !== 1) throw new Error(`${found.length} cards for ${machine}`);
  return found[0] as HTMLElement;
};

export const rowsOf = (scope: Element): Element[] =>
  all(scope, {role: 'row'}).filter(row => all(row, {role: 'columnheader'}).length === 0);

export const testOf = (shell: OpenShell, machine: string, cluster: string): HTMLElement => {
  const row = all(cardOf(shell, machine), {role: 'row', name: new RegExp(cluster)})[0];
  if (row === undefined) throw new Error(`no row for ${cluster}`);
  return one(row, {role: 'button'});
};

export const rowFor = (shell: OpenShell, machine: string, cluster: string): Element => {
  const row = all(cardOf(shell, machine), {role: 'row', name: new RegExp(cluster)})[0];
  if (row === undefined) throw new Error(`no row for ${cluster}`);
  return row;
};

export const rail = (shell: OpenShell): HTMLElement =>
  one(shell.container, {role: 'navigation', name: 'VibeSys'});

/** The rail's Machines button carries its warning in its name: the word the machine's card uses. */
export const warning = (shell: OpenShell, word = 'Unreachable'): HTMLElement | null =>
  maybe(rail(shell), {role: 'button', name: new RegExp(`^Machines, ${word}$`)});

/** The page over `scripted`, once every machine's first answer is in. */
export async function openMachines(scripted: MachinesWorld): Promise<OpenShell> {
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
