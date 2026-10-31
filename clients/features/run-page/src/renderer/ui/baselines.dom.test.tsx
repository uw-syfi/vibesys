/**
 * The comparison baselines on the run page as drawn: the headline's `vs <label>`, the small status
 * labels, the chart's reference lines and the Ideas table's ratio columns, from the events of the
 * `comparison-baselines` scenario (vLLM measured, SGLang not measured, no reading of the input).
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {EMPTY_LEDGER, initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {IDEAS_FILTER_DEFAULT} from '../../state/ideas-view.js';
import {EMPTY_TRACE_LOG, traceStep} from '../../state/live.js';
import {createFactsReader} from '../../state/run-facts.js';
import {createRunReader} from '../../state/run-model.js';
import {Hero} from './hero.js';
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

const EVENTS = fixtureNamed('comparison-baselines').events;
const TRACE = traceStep(EMPTY_TRACE_LOG, {kind: 'events', events: EVENTS});
const CORE = reduceEventBatch(initialCoreState(), EVENTS);
const MODEL = createRunReader().read(
  TRACE,
  null,
  null,
  createFactsReader()({core: CORE, trace: TRACE, heard: null}),
);
const BASE = {
  model: MODEL,
  clock: {start: 0, now: null},
  tokens: EMPTY_LEDGER,
  who: new Map(),
  zoom: () => undefined,
};

async function drawn(
  node: ReactNode,
): Promise<{container: HTMLElement; done: () => Promise<void>}> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(node));
  return {
    container,
    done: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

test('the headline reads vs the comparison, the chart draws its line, the others are small labels', async () => {
  const screen = await drawn(<Hero {...BASE} whole={MODEL} idea={null} />);
  const text = screen.container.textContent ?? '';
  expect(text).toContain('5.82×');
  expect(text).toContain('vs vLLM');
  expect(text).not.toContain('than input');
  expect(text).toContain('SGLang · not measured');
  const lines = [...screen.container.querySelectorAll('[data-reference]')];
  expect(lines.map(line => line.textContent)).toEqual(['vLLM']);
  await screen.done();
});

test('the Ideas table has a ratio column for each comparison, a dash where there is none', async () => {
  const screen = await drawn(
    <IdeasTable
      base={BASE}
      filter={IDEAS_FILTER_DEFAULT}
      sort={null}
      setSort={() => undefined}
      selected={null}
      dispatch={() => undefined}
    />,
  );
  const header = [...screen.container.querySelectorAll('.vs-columnlist-headcell')].map(
    column => column.textContent,
  );
  expect(header.slice(header.indexOf('vs input'), header.indexOf('vs input') + 3)).toEqual([
    'vs input',
    'vLLM',
    'SGLang',
  ]);
  const at = header.indexOf('vs input');
  const rows = [...screen.container.querySelectorAll('[role="option"]')].map(row => ({
    title: row.querySelector('.vs-columnlist-cell .vs-text')?.textContent,
    cells: [...row.querySelectorAll('.vs-columnlist-cell')]
      .slice(at, at + 3)
      .map(cell => cell.textContent),
  }));
  expect(rows).toEqual([
    // The run has no reading of its input: `vs input` is a dash and the comparison carries the ratio.
    {title: 'Pin the consumers to cores', cells: ['–', '5.82×', '–']},
    {title: 'Preallocate the arena', cells: ['–', '5.29×', '–']},
  ]);
  await screen.done();
});
