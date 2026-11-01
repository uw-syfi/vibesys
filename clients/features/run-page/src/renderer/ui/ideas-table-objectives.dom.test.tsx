/**
 * The Ideas table of a run of one, two, three and six objectives, as the DOM draws it: every
 * objective is a cell that survives the narrow form, as its multiple (a run of one objective states
 * it in its cell, where its `vs input` column has given way), a column past the fourth is counted in
 * a header, the exact figures are the rows' description, and only the rows on the way to the
 * selected idea light their Built on link. The container queries themselves are CSS: these tests
 * hold what the markup says to them.
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {endOf, IDEAS_FILTER_DEFAULT} from '../../state/index.js';
import {type ShapedRun, wholeOf} from '../../state/shaped-runs.test-helper.js';
import {IdeasTable} from './ideas-table.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

const NAMES = ['throughput', 'p99_latency', 'ttft_ms', 'tpot_ms', 'memory_mb', 'cost'] as const;

/** A finished run of `count` objectives that all move: five ideas, the first three set a best. */
function runOf(count: number, inputKnown = true): ShapedRun {
  const ratios = (at: number): number[] =>
    NAMES.slice(0, count).map((_, k) => (k % 2 === 0 ? 1 + at / 10 : 1 - at / 20));
  return {
    objectives: NAMES.slice(0, count).map((name, k) => ({
      name,
      direction: k % 2 === 0 ? 'max' : 'min',
      input: 50000,
    })),
    inputKnown,
    ideas: [1, 2, 3, 4, 5].map((at, index) => ({
      passed: index !== 4,
      ratios: ratios(at),
      again: null,
    })),
    cursor: 1,
    moment: 0,
  };
}

async function drawn(run: ShapedRun, selected: string | null = null) {
  const whole = wholeOf(run);
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const base = {
    model: whole,
    position: endOf(whole),
    clock: {start: 0, now: null},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: () => undefined,
  };
  await act(async () =>
    root.render(
      <IdeasTable
        base={base}
        filter={IDEAS_FILTER_DEFAULT}
        sort={null}
        setSort={() => undefined}
        selected={selected}
        dispatch={() => undefined}
      />,
    ),
  );
  return {
    container,
    done: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

const heads = (container: HTMLElement) => [
  ...container.querySelectorAll('.vs-columnlist-headcell'),
];
const named = (container: HTMLElement, name: string) =>
  heads(container).find(each => (each.textContent ?? '').startsWith(name));

test('every objective of a run of two, three or six keeps its cell when the list is narrow, as a multiple', async () => {
  for (const count of [2, 3, 6]) {
    const screen = await drawn(runOf(count));
    const objectives = heads(screen.container).filter(each =>
      NAMES.some(name =>
        (each.textContent ?? '').toLowerCase().startsWith(name.split('_')[0] ?? ''),
      ),
    );
    expect(objectives.length).toBe(Math.min(count, 4));
    // Only the columns the narrow form can do without are optional: the idea's built on, its agent time.
    for (const head of objectives) expect(head.hasAttribute('data-optional')).toBe(false);
    for (const row of screen.container.querySelectorAll('[role="option"]')) {
      const cells = [...row.querySelectorAll('.vs-columnlist-cell:not([data-optional])')];
      // The idea, the status, and a cell with a multiple for each objective.
      expect(cells.length).toBe(2 + Math.min(count, 4));
      for (const cell of cells.slice(2)) expect(cell.textContent).toMatch(/×|–/);
    }
    await screen.done();
  }
});

test('a run of one objective states its multiple in its cell for the narrow form, and in its own column while wide', async () => {
  const screen = await drawn(runOf(1));
  const first = screen.container.querySelector('[role="option"]');
  const cell = first?.querySelector('[data-column="objective-0"]');
  // The wide form: the value, then its multiple apart for the narrow form alone.
  expect(cell?.querySelector('[data-optional]')?.textContent).toMatch(/^\d/);
  expect(cell?.querySelector('[data-narrow-only]')?.textContent).toMatch(/×$/);
  const vsInput = first?.querySelector('[data-column="vs-input"]');
  expect(vsInput?.hasAttribute('data-optional')).toBe(true);
  expect(vsInput?.textContent).toBe(cell?.querySelector('[data-narrow-only]')?.textContent);
  await screen.done();
});

test('a run with no input has no vs input column, no share of gain, and keeps its values in the narrow form', async () => {
  const screen = await drawn(runOf(1, false));
  expect(named(screen.container, 'vs input')).toBeUndefined();
  expect(named(screen.container, 'Share of gain')).toBeUndefined();
  const cell = screen.container.querySelector('[role="option"] [data-column="objective-0"]');
  // No multiple to give way to: the value stays when the list is narrow.
  expect(cell?.querySelector('[data-optional]')).toBeNull();
  expect(cell?.textContent).toMatch(/\d/);
  await screen.done();
});

test('objectives past the fourth are counted in the header of the last column', async () => {
  const screen = await drawn(runOf(6));
  const notes = [...screen.container.querySelectorAll('.vs-columnlist-note')];
  expect(notes.map(each => each.textContent)).toEqual(['+2']);
  expect(notes[0]?.getAttribute('title')).toBe('2 more objectives');
  expect(notes[0]?.parentElement?.textContent).toMatch(/^Tpot ms/);
  await screen.done();
  const four = await drawn(runOf(4));
  expect(four.container.querySelectorAll('.vs-columnlist-note')).toHaveLength(0);
  await four.done();
});

test('a row says its exact figures whole, each under its objective, while its cells are compact', async () => {
  const screen = await drawn(runOf(2));
  const row = screen.container.querySelector('[role="option"]');
  // Its name is the idea's; the rest it says is its status, what it built on, and its figures.
  expect(row?.getAttribute('aria-description')).toMatch(
    /, built on .*, Throughput [\d,]+ .*, P99 latency [\d,]+/,
  );
  const cell = row?.querySelector('[data-column="objective-0"]');
  expect(cell?.getAttribute('title')).toMatch(/^[\d,.]+/);
  await screen.done();
});

test('the idea’s own figures are compact from a thousand on, the exact figure the hint', async () => {
  const run = runOf(1);
  const screen = await drawn({
    ...run,
    objectives: [{...(run.objectives[0] as ShapedRun['objectives'][number]), input: 1581539}],
  });
  const cell = screen.container.querySelector('[role="option"] [data-column="objective-0"]');
  expect(cell?.textContent).toMatch(/^\d\.\d\dM/);
  expect(cell?.getAttribute('title')).toMatch(/^\d,\d{3},\d{3}/);
  await screen.done();
});

test('only the rows on the way to the selected idea light their Built on link', async () => {
  const run = runOf(2);
  const screen = await drawn(run, 'i3');
  const lit = [...screen.container.querySelectorAll('.vs-columnlist-link[data-lit]')];
  const links = [...screen.container.querySelectorAll('.vs-columnlist-link')];
  // The generated ideas build on no idea, so no row has a link to light: the input is no link.
  expect(links).toHaveLength(0);
  expect(lit).toHaveLength(0);
  await screen.done();
});

test('the table has no heading of its own: the chips are the first thing in it', async () => {
  const screen = await drawn(runOf(2));
  expect(screen.container.querySelector('.vs-text[data-variant="heading"]')).toBeNull();
  expect(
    screen.container.firstElementChild?.querySelector('button.vs-dotlegend-item'),
  ).not.toBeNull();
  await screen.done();
});
