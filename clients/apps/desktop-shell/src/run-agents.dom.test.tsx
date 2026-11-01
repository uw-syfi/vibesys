/**
 * The Agents face of the Run screen on the agents journey (a run whose span records name each agent
 * turn's agent and who started it): the tree of who started whom (with the judge's fan-in), an agent
 * opened beside the tree with its turns, and a turn opened as a step. The turns report what they
 * spent: the run's total sits in the header, each agent's in the tree and its pane, each turn's in
 * its row and its pane header. The Usage face reads turns that carry the per-model split without
 * moving a figure. The shell is drawn in a DOM over the journeys' Fake gateway; the layout of each
 * level is the browser's (`e2e/run-agents.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {press} from './testing/dom-controls.js';
import {all, maybe, one, see, textOf, until} from './testing/dom-query.js';
import {journeyFixture, journeyWorld} from './testing/journey-records.js';
import {shellOnRun} from './testing/run-screen.js';
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

async function opened(): Promise<HTMLElement> {
  shell = await shellOnRun(
    journeyWorld('agents'),
    new FakeClock().schedule,
    journeyFixture('agents'),
  );
  return shell.container;
}

const show = (container: Element, face: string) =>
  press(
    one(one(container, {role: 'radiogroup', name: 'Show'}), {
      role: 'radio',
      name: face,
      exact: true,
    }),
  );

const statOf = (container: Element, label: string): Element => {
  const stat = [...container.querySelectorAll('.vs-stat')].find(each =>
    textOf(each).includes(label),
  );
  if (stat === undefined) throw new Error(`the screen has no ${label} stat`);
  return stat;
};

test('the agents tree shows who started whom, and an agent opens beside it with its turns', async () => {
  const container = await opened();
  await show(container, 'Agents');
  const agents = one(container, {role: 'listbox', name: 'Agents'});
  expect(all(agents, {role: 'option'})).toHaveLength(6);
  expect(textOf(all(agents, {role: 'option'})[0] as Element)).toContain('Planner');
  expect(textOf(one(agents, {role: 'option', name: /reviewer/i}))).toContain(
    'also started by Builder',
  );
  await see(container, 'agent · what it is doing', true);

  await press(all(agents, {role: 'option', name: /^Done Builder/})[0] as HTMLElement);
  const turns = one(container, {role: 'listbox', name: 'Turns'});
  expect(all(turns, {role: 'option'})).toHaveLength(1);
  one(container, {role: 'button', name: 'Planner'});

  // A turn opens as a step, beside the agents.
  await press(all(turns, {role: 'option'})[0] as HTMLElement);
  one(container, {role: 'listbox', name: 'Agents'});
  await until(
    'the turn opened as a step',
    () => maybe(container, {role: 'listbox', name: 'Turns'}) === null,
  );
});

test('the run, each agent and each turn name the tokens they spent', async () => {
  const container = await opened();
  const total = statOf(container, 'Tokens');
  expect(textOf(total)).toMatch(/1\.5M input\s*· 1\.3M cached · 51\.1k output/);
  expect(total.getAttribute('title')).toBe(
    'Input 1,466,600 (1,299,500 cached, 167,100 uncached)\nOutput 51,100 (17,100 reasoning)\nTotal 1,517,700',
  );

  await show(container, 'Agents');
  const agents = one(container, {role: 'listbox', name: 'Agents'});
  await see(container, 'in (cached)', true);
  // Each agent's turns summed: the first implementer's one turn, the planner's one measured turn,
  // input with its cached share beside it, then output.
  expect(textOf(all(agents, {role: 'option', name: /builder/i})[0] as Element)).toContain(
    '1.2M 1.1M cached',
  );
  expect(textOf(all(agents, {role: 'option', name: /planner/i})[0] as Element)).toContain('182.4k');

  // W5's implementer: its pane totals it, and its turn opens with its tokens in the header.
  await press(all(agents, {role: 'option', name: /builder/i})[1] as HTMLElement);
  await see(container, 'in 48.3k (31k cached) · out 2.6k');
  await press(
    all(one(container, {role: 'listbox', name: 'Turns'}), {role: 'option'})[0] as HTMLElement,
  );
  await see(container, /· in 48\.3k \(31k cached\) · out 2\.6k$/);
});

test('the Usage face reads turns that carry the per-model split, and the split moves no figure', async () => {
  // The planner's turn ran a model the price table does not know, so it has no cost; the judge's
  // turn ran on a priced model and its cost is an amount.
  const container = await opened();
  await show(container, 'Usage');
  await until(
    'the usage table',
    () => maybe(container, {role: 'table', name: 'Usage by group'}) !== null,
  );
  const table = one(container, {role: 'table', name: 'Usage by group'});
  const cost = (group: string): string =>
    textOf(
      (
        [...table.querySelectorAll('tbody tr')].find(row => textOf(row).includes(group)) as Element
      ).querySelectorAll('td')[6] as Element,
    );
  expect(cost('Reviewer')).toBe('$0.02');
  expect(cost('Planner')).toBe('unknown');
  expect(cost('Builder')).toBe('unknown');
  expect(textOf(one(container, {role: 'region', name: 'Totals'}))).toContain(
    'in 1.5M · out 34k · reasoning 17.1k',
  );
});
