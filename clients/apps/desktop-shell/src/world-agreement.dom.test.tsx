/**
 * One world, one set of counts: for every run fixture, the machines and runs that the Runs
 * dashboard, Settings' Machines and New run show agree. A machine that has a run, or that New run
 * offers, is never "No machine yet", and a machine with a run is never missing from Settings'
 * Machines. The screens are the shell's own, drawn in a DOM over the fixture's world; the same
 * check ran in Chromium once per fixture, about a second each.
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {FIXTURE_NAMES, fixtureNamed} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {pickLabel, press} from './testing/dom-controls.js';
import {all, one, see, setChecked, settle, textOf, until} from './testing/dom-query.js';
import {installRequestBound} from './testing/fake-bridge.js';
import {seedOf} from './testing/fixture-seed.js';
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

/** How often a page may ask the bridge for the same thing; far above any screen's own reads. */
const REQUEST_LIMIT = 100;

const rail = (shell: OpenShell): HTMLElement =>
  one(shell.container, {role: 'navigation', name: 'VibeSys'});

async function showScreen(shell: OpenShell, destination: string): Promise<void> {
  await press(one(rail(shell), {role: 'button', name: destination, exact: destination === 'New'}));
  await shell.read();
}

/** The machines the dashboard shows runs under, with how many runs each. */
async function dashboardRuns(shell: OpenShell): Promise<Map<string, number>> {
  await showScreen(shell, 'Runs');
  one(shell.container, {role: 'heading', name: 'Runs'});
  await pickLabel(
    one(shell.container, {role: 'combobox', name: 'Group by'}) as HTMLSelectElement,
    'Machine',
  );
  await settle();
  const runs = new Map<string, number>();
  for (const group of shell.container.querySelectorAll('.vs-overview-group')) {
    const title = group.getAttribute('aria-label') ?? '';
    if (title === 'Needs you') continue;
    runs.set(title, (runs.get(title) ?? 0) + group.querySelectorAll('.vs-overview-tile').length);
  }
  return runs;
}

/**
 * The section's button as the wide window lists it, not the disclosure that stands for the list
 * when the window is narrow (it holds the same words and says what it controls).
 */
const machinesSection = (shell: OpenShell): HTMLElement => {
  const nav = one(shell.container, {role: 'navigation', name: 'Settings sections'});
  const found = all(nav, {role: 'button', name: /Machines/}).filter(
    each => !each.hasAttribute('aria-controls'),
  );
  if (found.length !== 1) throw new Error(`${found.length} Machines sections`);
  return found[0] as HTMLElement;
};

async function machinesListed(shell: OpenShell): Promise<string[]> {
  await showScreen(shell, 'Settings');
  await press(machinesSection(shell));
  await shell.read();
  await see(shell.container, 'Where runs can go', true);
  await until('the machines read', () => all(shell.container, {role: 'treeitem'}).length > 0);
  const tree = one(shell.container, {role: 'tree', name: 'Machines'});
  return all(tree, {role: 'treeitem'}).map(row =>
    (row.querySelector('.vs-tree-text > *')?.textContent ?? '').trim(),
  );
}

test('the machines and runs agree across every screen, for every fixture', async () => {
  for (const name of FIXTURE_NAMES) {
    const shell = await openShell(
      {kind: 'runs'},
      {
        schedule: new FakeClock().schedule,
        seed: seedOf(fixtureNamed(name), 'ink'),
        installs: [() => installRequestBound(REQUEST_LIMIT)],
      },
    );
    shells.push(shell);
    const onDashboard = await dashboardRuns(shell);
    expect(onDashboard.size, name).toBeGreaterThan(0);

    const machines = await machinesListed(shell);
    expect(textOf(machinesSection(shell)), name).toContain(`${machines.length} saved`);
    expect(textOf(shell.container), name).not.toContain('No machine yet');
    for (const machine of onDashboard.keys()) expect(machines, name).toContain(machine);

    await showScreen(shell, 'New');
    await setChecked(one(shell.container, {role: 'radio', name: /Your project/}), true);
    await press(one(shell.container, {role: 'button', name: 'Continue', exact: true}));
    await settle();
    const offered = all(one(shell.container, {role: 'combobox', name: 'Machine'}), {
      role: 'option',
    }).map(option => textOf(option));
    expect([...offered].sort(), name).toEqual([...machines].sort());
    const loops = (globalThis.window as unknown as {fakeHost: {requestLoops?: string[]}}).fakeHost
      .requestLoops;
    expect(loops, `${name}: requests the page repeated without end`).toEqual([]);
    await shell.close();
    shells.pop();
  }
});
