/**
 * The Run screen's levels on the trace journey: the run level (the best ratio, the ideas running
 * first with what they do, the dropped chip and the search), an idea, a step and a step's item
 * reached by click and walked back by Escape, the breadcrumb and the window's Back, the chart's
 * ring and dots, the Logs face by owner and the timeline. The shell is drawn in a DOM over the
 * journeys' Fake gateway; the layout of each level, the keyboard walk and the live turn's scroll
 * are the browser's (`e2e/run-levels.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {fillIn, press} from './testing/dom-controls.js';
import {
  all,
  byText,
  field,
  maybe,
  notSee,
  one,
  see,
  settle,
  textOf,
  until,
} from './testing/dom-query.js';
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
  if (shell !== undefined) await settle();
  await shell?.close();
  shell = undefined;
});

async function opened(): Promise<HTMLElement> {
  shell = await shellOnRun(
    journeyWorld('trace'),
    new FakeClock().schedule,
    journeyFixture('trace'),
  );
  return shell.container;
}

const IDEA = 'Combine W1 ring with W2 padding';
const ideasOf = (container: Element) => one(container, {role: 'listbox', name: 'Ideas'});
const show = (container: Element, face: string) =>
  press(
    one(one(container, {role: 'radiogroup', name: 'Show'}), {
      role: 'radio',
      name: face,
      exact: true,
    }),
  );
const go = (way: 'back' | 'forward'): Promise<void> => act(async () => shell?.address[way]());
const pressEscape = async (): Promise<void> => {
  await act(async () => {
    window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  });
};
const atRun = (container: Element): boolean =>
  maybe(container, {role: 'listbox', name: 'Ideas'}) !== null &&
  maybe(container, {role: 'heading', name: IDEA}) === null;

const chip = (container: Element, label: RegExp): HTMLElement => {
  const found = [...container.querySelectorAll('button.vs-dotlegend-item')].find(each =>
    label.test(textOf(each)),
  );
  if (found === undefined) throw new Error(`no chip ${String(label)}`);
  return found as HTMLElement;
};

test('the run level shows the best ratio, and the ideas running first with what they do', async () => {
  const container = await opened();
  const ideas = ideasOf(container);
  // Running first, then the rest by the first objective, best first; the ones with no reading last.
  const rows = all(ideas, {role: 'option'}).map(textOf);
  const order = [
    /^Per-core queues with stealing/,
    /^Wait-free consumer path/,
    /^Combine W1 ring with W2 padding/,
    /^Lock-free ring with per-slot sequence numbers/,
    /^Cache-line padding and batched enqueue/,
    /^Larger batch size/,
    /^Spin then park idle consumers/,
  ];
  expect(rows).toHaveLength(order.length);
  for (const [index, pattern] of order.entries()) expect(rows[index]).toMatch(pattern);
  await see(container, '1.18×');
  // A running idea says what it is doing under its name.
  expect(textOf(one(ideas, {role: 'option', name: /Combine W1 ring/}))).toMatch(
    /correctness \+ benchmark/,
  );
});

test('the dropped chip narrows the list, a second press brings the rest back, and the search narrows it', async () => {
  const container = await opened();
  const ideas = ideasOf(container);
  await press(chip(container, /^Dropped 1/));
  one(ideas, {role: 'option', name: /Spin then park/});
  expect(maybe(ideas, {role: 'option', name: /Combine W1 ring/})).toBeNull();
  await press(chip(container, /^Dropped 1/));
  one(ideas, {role: 'option', name: /Combine W1 ring/});
  await fillIn(field(container, 'Search ideas') as HTMLInputElement, 'spin');
  expect(all(ideas, {role: 'option'}).map(textOf)).toEqual([
    expect.stringMatching(/Spin then park/),
  ]);

  // The mode switch keeps the filter in the page state.
  await show(container, 'Agents');
  await show(container, 'Ideas');
  expect((field(container, 'Search ideas') as HTMLInputElement).value).toBe('spin');
});

test('every level opens by click and walks back by Escape, the crumbs and Back', async () => {
  const container = await opened();
  const heading = () => maybe(container, {role: 'heading', name: IDEA});
  const turn = () => maybe(container, {role: 'region', name: 'Agent turn'});
  const itemText = 'queue/mpmc time: 41.2 ms';
  const itemShown = () => byText(container, itemText).length > 0;

  // Level 1: the idea beside the ideas, with what it built on.
  await press(one(ideasOf(container), {role: 'option', name: new RegExp(IDEA)}));
  await until('the idea', () => heading() !== null);
  expect(byText(container, /^from$/)).toHaveLength(0);

  // Level 2: a step beside the idea's steps; the turn reads as narrative, and a group opens and closes.
  const steps = one(container, {role: 'listbox', name: /steps$/});
  await press(all(steps, {role: 'option', name: /builder/i})[0] as HTMLElement);
  await until('the turn', () => turn() !== null);
  const step = turn() as HTMLElement;
  await see(step, 'The head and tail counters share a cache line.');
  await see(step, 'I will pad each counter to its own line');
  const group = one(step, {role: 'button', name: /1 tool/});
  expect(group.getAttribute('aria-expanded')).toBe('true');
  await press(group);
  expect(group.getAttribute('aria-expanded')).toBe('false');
  await press(group);
  expect(group.getAttribute('aria-expanded')).toBe('true');

  // Level 3: the item in full, beside its step, whose group stays open with the call marked.
  await press(all(step, {role: 'button', name: /cargo bench --bench queue/}).at(-1) as HTMLElement);
  await until('the item', itemShown);
  expect(turn()?.querySelector('[aria-current="true"]')).not.toBeNull();

  // Escape goes up a level; Back retraces the zooms and Forward goes on again.
  await pressEscape();
  await until('the step', () => !itemShown());
  expect(turn()).not.toBeNull();
  await go('back');
  await until('the item again', itemShown);
  await go('forward');
  await until('the step again', () => !itemShown());
  expect(turn()).not.toBeNull();

  // Each crumb is a zoom the address follows, so Back returns to the level it left.
  const crumbs = () => one(container, {role: 'navigation', name: 'Zoom'});
  await press(one(crumbs(), {role: 'button', name: IDEA}));
  await until('the idea', () => turn() === null);
  expect(heading()).not.toBeNull();
  await go('back');
  await until('the step', () => turn() !== null);
  await press(one(crumbs(), {role: 'button', name: 'Run', exact: true}));
  await until('the run level', () => atRun(container));
  await go('back');
  await until('the step', () => turn() !== null);
  await go('forward');
  await until('the run level', () => atRun(container));

  // A running idea's row opens its idea; Escape walks up to the run.
  await press(all(ideasOf(container), {role: 'option', name: /Combine W1 ring/})[0] as HTMLElement);
  await until('the idea', () => heading() !== null);
  await pressEscape();
  await until('the run level', () => atRun(container));
});

/** The readings ringed in the text color (the open idea's) and in the accent (its lineage's steps). */
function ringed(container: Element): {selected: string[]; lineage: string[]} {
  const chart = one(container, {role: 'group', name: /of each reading over the run/});
  const named = (ring: string): string[] =>
    [...chart.querySelectorAll('.vs-chart-pick')]
      .filter(point => point.querySelector(ring) !== null)
      .map(point => (point.getAttribute('aria-label') ?? '').split(' · ')[0] ?? '');
  return {selected: named('.vs-chart-ring'), lineage: named('.vs-chart-lit')};
}

test('an open idea is singled out on the chart: its reading ringed, the steps of its lineage ringed in the accent', async () => {
  const container = await opened();
  const key = () => one(container, {role: 'list', name: 'Chart key'});
  const selectedKey = () => byText(key(), 'Selected', true).length > 0;
  expect(ringed(container)).toEqual({selected: [], lineage: []});
  expect(selectedKey()).toBe(false);

  // A measured idea: its own reading is ringed, every reading stays, and the key names the ring.
  await press(one(ideasOf(container), {role: 'option', name: /Cache-line padding/}));
  await until('the ring', () => ringed(container).selected.length > 0);
  expect(ringed(container).selected).toEqual(['Cache-line padding and batched enqueue']);
  expect(selectedKey()).toBe(true);
  expect(
    all(one(container, {role: 'group', name: /of each reading over the run/}), {role: 'button'}),
  ).toHaveLength(3);
  await see(container, '1.18×');

  // An idea with no measurement has no reading to ring, so the key does not name a ring.
  await pressEscape();
  await until('the run level', () => atRun(container));
  await press(one(ideasOf(container), {role: 'option', name: new RegExp(IDEA)}));
  await until('the idea', () => maybe(container, {role: 'heading', name: IDEA}) !== null);
  expect(ringed(container).selected).toEqual([]);
  expect(selectedKey()).toBe(false);

  // Back at the run nothing is ringed.
  await pressEscape();
  await until('the run level', () => atRun(container));
  expect(ringed(container)).toEqual({selected: [], lineage: []});
});

test('clicking a dot of the chart opens its idea', async () => {
  const container = await opened();
  const chart = () => one(container, {role: 'group', name: /of each reading over the run/});
  await press(one(chart(), {role: 'button', name: /^Cache-line padding and batched enqueue ·/}));
  await until(
    'its idea',
    () =>
      maybe(container, {role: 'heading', name: 'Cache-line padding and batched enqueue'}) !== null,
  );
  // The chart stays; another dot opens another idea beside the same chart.
  await press(one(chart(), {role: 'button', name: /^Larger batch size ·/}));
  await until(
    'the other idea',
    () => maybe(container, {role: 'heading', name: 'Larger batch size'}) !== null,
  );
});

test('the Logs face lists the run’s streams by owner, and an idea shows only its own', async () => {
  const container = await opened();
  await show(container, 'Logs');
  const streams = one(container, {role: 'listbox', name: 'Log streams'});
  expect(all(streams, {role: 'option'})).toHaveLength(2);
  await press(one(streams, {role: 'option', name: /benchmark/}));
  await until('the log', () => maybe(container, {role: 'log', name: /benchmark/}) !== null);
  await until('its text', () =>
    textOf(one(container, {role: 'log', name: /benchmark/})).includes('repetition 1: 41.2 Mops/s'),
  );

  // The idea that owns the benchmark gate shows that stream alone: no list, one pane.
  await show(container, 'Ideas');
  await press(one(ideasOf(container), {role: 'option', name: new RegExp(IDEA)}));
  const pane = await paneOf(container);
  await press(one(one(pane, {role: 'radiogroup', name: 'Show'}), {role: 'radio', name: 'Logs'}));
  await until('the idea log', () =>
    textOf(one(container, {role: 'log', name: /benchmark/})).includes('repetition 1'),
  );
  expect(maybe(container, {role: 'listbox', name: 'Log streams'})).toBeNull();
});

async function paneOf(container: Element): Promise<Element> {
  await until(
    'the idea pane',
    () => container.querySelector('.vs-pane[data-width="fill"]') !== null,
  );
  return container.querySelector('.vs-pane[data-width="fill"]') as Element;
}

test('the timeline draws one row per idea; a segment opens its step, a row its idea', async () => {
  const container = await opened();
  await show(container, 'Timeline');
  const gantt = () => one(container, {role: 'listbox', name: 'Ideas on the timeline'});
  // The Ideas list's order: running first, then the rest newest first.
  const order = [
    /Per-core queues with stealing/,
    /Wait-free consumer path/,
    /Combine W1 ring with W2 padding/,
    /Larger batch size/,
    /Spin then park/,
    /Cache-line padding and batched enqueue/,
    /Lock-free ring with per-slot sequence numbers/,
  ];
  const rows = all(gantt(), {role: 'option'}).map(textOf);
  expect(rows).toHaveLength(order.length);
  for (const [index, pattern] of order.entries()) expect(rows[index]).toMatch(pattern);
  const range = one(container, {role: 'radiogroup', name: 'Range'});
  await press(one(range, {role: 'radio', name: 'Last hour'}));
  expect((one(range, {role: 'radio', name: 'Last hour'}) as HTMLInputElement).checked).toBe(true);
  await press(one(range, {role: 'radio', name: 'All', exact: true}));

  // A segment opens its step, beside the idea's steps.
  const row = one(gantt(), {role: 'option', name: /Combine W1 ring/});
  await press(row.querySelector('.vs-gantt-seg') as Element);
  await until('the steps', () => maybe(container, {role: 'listbox', name: /steps$/}) !== null);
  await pressEscape();

  // Up from a step is its idea: its steps and the strip of them over time.
  await until(
    'the idea steps',
    () => maybe(container, {role: 'listbox', name: `${IDEA} steps`, exact: true}) !== null,
  );
  one(container, {role: 'listbox', name: /steps over time$/});

  // A row opens its idea.
  await pressEscape();
  await show(container, 'Timeline');
  await press(one(gantt(), {role: 'option', name: /Wait-free consumer path/}));
  await until(
    'the idea',
    () => maybe(container, {role: 'heading', name: 'Wait-free consumer path'}) !== null,
  );
});

test('an idea’s Logs face shows its stream without a pick: the page follows what it shows', async () => {
  const container = await opened();
  await press(one(ideasOf(container), {role: 'option', name: new RegExp(IDEA)}));
  const pane = await paneOf(container);
  await press(one(one(pane, {role: 'radiogroup', name: 'Show'}), {role: 'radio', name: 'Logs'}));
  await until('the stream', () =>
    textOf(one(container, {role: 'log', name: /benchmark/})).includes('repetition 1: 41.2'),
  );
  notSee(container, 'Choose a stream');
});
