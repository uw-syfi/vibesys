/**
 * The Usage face of the Run screen on a finished run whose turns report what they spent and on which
 * harness and model they ran: its tiles say what the header says, every control changes what the
 * face shows, and the header, the Agents list and an idea's steps name what ran them. The shell is
 * drawn in a DOM over the journeys' Fake gateway; where the controls and the table's columns sit at
 * each width is the browser's (`e2e/run-usage.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {press} from './testing/dom-controls.js';
import {
  all,
  checked,
  field,
  maybe,
  notSee,
  one,
  pickValue,
  see,
  setChecked,
  textOf,
  until,
} from './testing/dom-query.js';
import {fixtureWorld, shellOnRun} from './testing/run-screen.js';
import type {OpenShell} from './testing/shell-world.js';

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
  for (const name of [...installed, 'window', 'indexedDB', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

let shell: OpenShell | undefined;
afterEach(async () => {
  await shell?.close();
  shell = undefined;
});

const FIXTURE = fixtureNamed('finished-ten-ideas');

async function opened(): Promise<HTMLElement> {
  shell = await shellOnRun(fixtureWorld(FIXTURE), new FakeClock().schedule, FIXTURE);
  one(shell.container, {role: 'slider', name: 'Run position'});
  return shell.container;
}

const choose = (scope: Element, group: string, label: string) =>
  press(
    one(one(scope, {role: 'radiogroup', name: group}), {role: 'radio', name: label, exact: true}),
  );

async function showUsage(container: Element): Promise<void> {
  await choose(container, 'Show', 'Usage');
  await until(
    'the usage table',
    () => maybe(container, {role: 'table', name: 'Usage by group'}) !== null,
  );
}

/** The rows' first cells: the groups the table lists, the total last. */
const groups = (container: Element): string[] =>
  [
    ...one(container, {role: 'table', name: 'Usage by group'}).querySelectorAll(
      'tbody tr td:first-child',
    ),
  ].map(cell => textOf(cell));

test('the Usage tiles say what the header says', async () => {
  const container = await opened();
  // The text is in the page at every width, though a narrow header hides what follows the figure.
  const tokens = [...container.querySelectorAll('.vs-stat')].find(each =>
    /Tokens/.test(textOf(each)),
  );
  await showUsage(container);
  const tiles = one(container, {role: 'region', name: 'Totals'});
  for (const name of ['Cost', 'Tokens', 'Cached input', 'Per idea']) await see(tiles, name, true);
  const stat = textOf(tokens ?? container);
  const input = /([\d.]+[kMB]?) input/.exec(stat)?.[1] ?? 'none';
  const cached = /([\d.]+[kMB]?) cached/.exec(stat)?.[1] ?? 'none';
  expect(textOf(tiles)).toContain(`in ${input} ·`);
  expect(textOf(tiles)).toContain(`${cached} of ${input} read from cache`);
});

test('every control of the Usage face changes what it shows', async () => {
  const container = await opened();
  await showUsage(container);
  const byRole = groups(container);
  expect(byRole.at(-1)).toBe('Run total');

  await choose(container, 'Group by', 'Harness');
  const byHarness = groups(container);
  expect(byHarness.length).toBe(3);
  expect(byHarness).not.toEqual(byRole);

  await choose(container, 'Group by', 'Idea');
  expect(groups(container).length).toBeGreaterThan(5);
  one(container, {role: 'columnheader', name: 'result'});
  const seconds = [...container.querySelectorAll('.vs-table tbody td:nth-child(2)')].map(textOf);
  expect(seconds).toContain('1.31×');

  await choose(container, 'Group by', 'Role');
  await pickValue(field(container, 'Then by', true) as HTMLSelectElement, 'model');
  await press(all(container, {role: 'button', name: /^Expand /})[0] as HTMLElement);
  expect(groups(container).length).toBeGreaterThan(byRole.length);

  await choose(container, 'Count', 'Tokens');
  one(container, {role: 'img', name: /in tokens/});
  expect(all(container, {role: 'checkbox'})).toHaveLength(4);
  for (const kind of ['Input', 'Cached', 'Output'])
    await setChecked(field(container, kind, true), false);
  expect(checked(field(container, 'Reasoning', true))).toBe(true);
  await press(field(container, 'Reasoning', true));
  expect(checked(field(container, 'Reasoning', true))).toBe(true);

  await choose(container, 'Chart', 'Running total');
  expect(container.querySelector('.vs-area')).not.toBeNull();
  await choose(container, 'Group by', 'Harness');
  const codex = [...container.querySelectorAll('tbody tr')].find(row => /codex/.test(textOf(row)));
  await press(codex?.querySelectorAll('td')[1] as HTMLElement);
  expect(container.querySelectorAll('tr[aria-current="true"]')).toHaveLength(1);
  expect(container.querySelector('.vs-area-layer[data-dim]')).not.toBeNull();
});

test('the header, the Agents list and an idea’s steps name the harness and model', async () => {
  const container = await opened();
  await see(one(container, {role: 'main'}), /on codex/);
  await choose(container, 'Show', 'Agents');
  const agents = one(container, {role: 'listbox', name: 'Agents'});
  const folded = all(agents, {role: 'option', name: /finished/});
  if (folded.length > 0) await press(folded[0] as HTMLElement);
  const rows = all(agents, {role: 'option'}).map(textOf);
  expect(rows.some(row => /on codex/.test(row))).toBe(true);
  expect(rows.some(row => /on claude/.test(row))).toBe(true);
  await choose(container, 'Show', 'Ideas');
  await press(
    one(one(container, {role: 'listbox', name: 'Ideas'}), {
      role: 'option',
      name: /^Pad the slot headers/,
    }),
  );
  const steps = one(container, {role: 'listbox', name: /steps$/});
  expect(textOf(all(steps, {role: 'option'})[0] as Element)).toMatch(/on (codex|claude)/);
  notSee(container, /Not reported/);
  expect(maybe(container, {role: 'heading', name: 'queue-rs · mpmc'})).not.toBeNull();
});
