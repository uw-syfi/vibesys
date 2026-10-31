import {afterAll, beforeAll, expect, test} from 'bun:test';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act, useState} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {modelOfShape} from '../../state/ideas-tree-fixtures.js';
import {
  IDEAS_FILTER_DEFAULT,
  type RunPageAction,
  runPageInitial,
  runPageStep,
} from '../../state/index.js';
import {IdeasTable} from './ideas-table.js';
import {RunLevel} from './run-level.js';

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

const SHAPE = {
  parents: [null, 0, 1, 0, 3, 3, 5],
  outcomes: ['passed', 'passed', 'passed', 'passed', 'failed', 'passed', 'passed'],
  best: 2,
} as const;

async function shown() {
  const container = document.createElement('div');
  document.body.append(container);
  const root: Root = createRoot(container);
  const shape = modelOfShape(SHAPE);
  const model = {
    ...shape,
    candidates: {...shape.candidates, best: {candidate: 'n2', adopted: true}},
  };
  const zoomed: unknown[] = [];
  const hovered: (string | null)[] = [];
  const base = {
    model,
    clock: {start: 0, now: null},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: (event: unknown) => zoomed.push(event),
  };
  const initial = runPageInitial({
    identity: {host: 'ssh:host-b', runId: 'run-1'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'host-b',
    unavailable: {},
  });
  let send: (action: RunPageAction) => void = () => undefined;
  function Harness() {
    const [state, setState] = useState(initial);
    send = action => {
      if (action.type === 'ideas-hover') hovered.push(action.idea);
      setState(now => runPageStep(now, action).state);
    };
    return <RunLevel state={state} base={base} core={null} mode="ideas" dispatch={send} />;
  }
  await act(async () => root.render(<Harness />));
  return {
    container,
    zoomed,
    hovered,
    press: async (action: RunPageAction) => act(async () => send(action)),
    done: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

const ROWS = '[aria-label="Ideas"] [role="option"]';

/** The text of the cell under `header` in each row. */
function column(container: HTMLElement, header: string): string[] {
  const heads = [...container.querySelectorAll('.vs-columnlist-headcell')].map(
    each => each.textContent ?? '',
  );
  const at = heads.indexOf(header);
  if (at < 0) throw new Error(`no column ${header}: ${heads.join(', ')}`);
  return [...container.querySelectorAll(ROWS)].map(
    row => row.querySelectorAll('.vs-columnlist-cell')[at]?.textContent ?? '',
  );
}

function chips(container: HTMLElement): {label: string; pressed: string | null}[] {
  return [...container.querySelectorAll('button.vs-dotlegend-item')].map(each => ({
    label: each.textContent ?? '',
    pressed: each.getAttribute('aria-pressed'),
  }));
}

function click(container: HTMLElement, label: string): Promise<void> {
  const chip = [...container.querySelectorAll('button.vs-dotlegend-item')].find(each =>
    (each.textContent ?? '').includes(label),
  );
  if (chip === undefined) throw new Error(`no chip ${label}`);
  return act(async () => {
    (chip as HTMLElement).click();
  });
}

test('ideas list the first objective best first, an idea with no reading last, each with status and cause', async () => {
  const screen = await shown();
  expect(column(screen.container, 'Idea')).toEqual([
    'idea 6',
    'idea 5',
    'idea 3',
    'idea 2Adopted',
    'idea 1',
    'idea 0',
    'idea 4',
  ]);
  expect(column(screen.container, 'Status')).toEqual([
    'Faster',
    'Faster',
    'Faster',
    'Faster',
    'Faster',
    'No gain',
    'DroppedTurned down',
  ]);
  expect(column(screen.container, 'vs input')).toEqual([
    '1.06×',
    '1.05×',
    '1.03×',
    '1.02×',
    '1.01×',
    'No clear change',
    '–',
  ]);
  await screen.done();
});

test('the chips count each status, press to narrow the list and press again to show every idea', async () => {
  const screen = await shown();
  expect(chips(screen.container)).toEqual([
    {label: 'All 7', pressed: 'true'},
    {label: 'Faster 5', pressed: 'false'},
    {label: 'No gain 1', pressed: 'false'},
    {label: 'Dropped 1', pressed: 'false'},
  ]);
  await click(screen.container, 'Dropped');
  expect(column(screen.container, 'Idea')).toEqual(['idea 4']);
  expect(chips(screen.container).map(each => each.pressed)).toEqual([
    'false',
    'false',
    'false',
    'true',
  ]);
  await click(screen.container, 'Dropped');
  expect(column(screen.container, 'Idea')).toHaveLength(7);
  await screen.done();
});

test('a search narrows the list, and one that matches nothing says No matches', async () => {
  const screen = await shown();
  await screen.press({type: 'ideas-filter', filter: {query: 'idea 4', status: null}});
  expect(column(screen.container, 'Idea')).toEqual(['idea 4']);
  await screen.press({type: 'ideas-filter', filter: {query: 'zzzz', status: null}});
  expect(screen.container.querySelectorAll(ROWS)).toHaveLength(0);
  expect(screen.container.textContent).toContain('No matches');
  await screen.done();
});

test('a header sorts by its column and the same header again restores the default order', async () => {
  const screen = await shown();
  const header = [...screen.container.querySelectorAll('.vs-columnlist-headcell button')].find(
    each => each.textContent?.startsWith('Agent time'),
  ) as HTMLElement;
  const marked = (): string[] =>
    [...screen.container.querySelectorAll('.vs-columnlist-headcell[data-marked]')].map(
      each => each.textContent ?? '',
    );
  expect(marked()).toEqual(['Metric']);
  await act(async () => header.click());
  expect(marked()).toEqual(['Agent time']);
  await act(async () => header.click());
  expect(marked()).toEqual(['Metric']);
  expect(column(screen.container, 'Idea')[0]).toBe('idea 6');
  await screen.done();
});

test('pressing a row opens its idea and the pointer on it names it for the charts', async () => {
  const screen = await shown();
  const row = screen.container.querySelectorAll(ROWS)[1] as HTMLElement;
  await act(async () => row.click());
  expect(screen.zoomed).toEqual([{type: 'open-idea', idea: 'n5'}]);
  await act(async () => {
    row.dispatchEvent(new window.MouseEvent('mouseover', {bubbles: true}) as unknown as Event);
    row.dispatchEvent(new window.MouseEvent('mouseenter') as unknown as Event);
  });
  expect(screen.hovered).toContain('n5');
  await screen.done();
});

test('the rows that built the selected idea carry a bar, and the selected row is marked', async () => {
  const container = document.createElement('div');
  document.body.append(container);
  const root: Root = createRoot(container);
  const base = {
    model: modelOfShape(SHAPE),
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
        selected="n6"
        dispatch={() => undefined}
      />,
    ),
  );
  const marked = (attribute: string): string[] =>
    [...container.querySelectorAll(`${ROWS}[${attribute}]`)].map(
      row => row.querySelector('.vs-columnlist-cell')?.textContent ?? '',
    );
  // idea 6 is built on idea 5, on idea 3, on idea 0 (the input).
  expect(marked('data-lineage')).toEqual(['idea 5', 'idea 3', 'idea 0']);
  expect(marked('aria-selected="true"')).toEqual(['idea 6']);
  await act(async () => root.unmount());
  container.remove();
});
