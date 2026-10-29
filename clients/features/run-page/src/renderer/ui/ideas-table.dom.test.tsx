import {afterAll, beforeAll, expect, test} from 'bun:test';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act, useState} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {modelOfShape} from '../../state/ideas-tree-fixtures.js';
import {ideasView, type RunPageAction, runPageInitial, runPageStep} from '../../state/index.js';
import {subtitleOf} from './ideas-table.js';
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

test('a failed idea turned down by its checks says only Failed; another reason is named', () => {
  const model = modelOfShape(SHAPE);
  const row = ideasView(model, {query: '', passed: true, running: true, failed: true}).rows.find(
    item => item.outcome === 'failed',
  );
  if (row === undefined) throw new Error('fixture needs a failed idea');
  const said = (label: string) =>
    subtitleOf({...row, state: {...row.state, label}}, new Map(), new Map());
  const from = subtitleOf(row, new Map(), new Map());
  expect(from).toMatch(/^From /);
  expect(said('Failed')).toBe(from);
  expect(said('Failed · no design')).toBe(`${from} · no design`);
});

test('the line under an idea carries its effect verdict, never its status again', () => {
  const model = modelOfShape(SHAPE);
  const rows = ideasView(model, {query: '', passed: true, running: true, failed: true}).rows;
  const passed = rows.find(item => item.outcome === 'passed');
  if (passed === undefined) throw new Error('fixture needs a passed idea');
  const from = subtitleOf(passed, new Map(), new Map());
  expect(from).toMatch(/^From /);
  expect(from.toLowerCase()).not.toContain('passed');
  expect(subtitleOf(passed, new Map(), new Map([[passed.id, 'within noise']]))).toBe(
    `${from} · within noise of its base`,
  );
});

async function shown(compared = false) {
  const container = document.createElement('div');
  document.body.append(container);
  const root: Root = createRoot(container);
  const shape = modelOfShape(SHAPE);
  // A compared idea has a ratio against its base (the run has none against the input).
  const fixture = compared
    ? {
        ...shape,
        candidates: {
          ...shape.candidates,
          candidates: shape.candidates.candidates.map(item =>
            item.id === 'n3' && item.measurement !== null
              ? {
                  ...item,
                  measurement: {
                    ...item.measurement,
                    vsBaseline: null,
                    against: {
                      ratio: 1.1,
                      baseCommit: item.attempts[0]?.revision ?? 'x',
                      baseValue: 100,
                      verdict: 'better' as const,
                    },
                  },
                }
              : item,
          ),
        },
      }
    : shape;
  const model = {
    ...fixture,
    candidates: {...fixture.candidates, best: {candidate: 'n2', adopted: true}},
  };
  const base = {
    model,
    clock: {start: 0, now: null},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: () => undefined,
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
    send = action => setState(now => runPageStep(now, action).state);
    return <RunLevel state={state} base={base} core={null} mode="ideas" dispatch={send} />;
  }
  await act(async () => root.render(<Harness />));
  return {
    container,
    press: async (action: RunPageAction) => act(async () => send(action)),
    done: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

function titles(container: HTMLElement): string[] {
  return [
    ...container.querySelectorAll(
      '[aria-label="Ideas"] [role="option"] .vs-pickrow-text > .vs-text:first-child',
    ),
  ].map(each => each.textContent ?? '');
}

test('ideas are flat, newest first, with an adopted pill and no lineage controls', async () => {
  const screen = await shown();
  expect(titles(screen.container)).toEqual([
    'idea 6',
    'idea 5',
    'idea 4',
    'idea 3',
    'idea 2',
    'idea 1',
    'idea 0',
  ]);
  expect(screen.container.querySelector('[aria-label="Ideas"] .vs-guide')).toBeNull();
  expect(screen.container.querySelector('[aria-label="Ideas"] .vs-pickrow-fold')).toBeNull();
  expect(screen.container.textContent).not.toContain('best path');
  expect(screen.container.textContent).not.toContain('from input');
  expect(
    screen.container.querySelectorAll('[aria-label="Ideas"] .vs-chip[data-tone="ok"]'),
  ).toHaveLength(6);
  expect(
    [...screen.container.querySelectorAll('[aria-label="Ideas"] .vs-chip[data-tone="ok"]')].filter(
      chip => chip.textContent === 'Adopted',
    ),
  ).toHaveLength(1);
  expect(
    [...screen.container.querySelectorAll('[aria-label="Ideas"] .vs-chip[data-tone="ok"]')].filter(
      chip => chip.textContent === 'Passed',
    ),
  ).toHaveLength(5);
  const adoptedRow = [
    ...screen.container.querySelectorAll('[aria-label="Ideas"] [role="option"]'),
  ].find(row => row.querySelector('.vs-chip[data-tone="ok"]') !== null);
  expect(
    [...(adoptedRow?.children ?? [])]
      .map(child => child.className)
      .filter(name => name === 'vs-chip' || name === 'vs-pickrow-figure'),
  ).toEqual(['vs-chip', 'vs-pickrow-figure']);
  expect(screen.container.querySelector('[aria-label="Ideas"] .vs-pickrow-measure')).toBeNull();
  await screen.done();
});

test('search shows matching ideas only and failed rows use one concise outcome', async () => {
  const screen = await shown();
  await screen.press({
    type: 'ideas-filter',
    filter: {query: 'idea 4', passed: true, running: true, failed: true},
  });
  expect(titles(screen.container)).toEqual(['idea 4']);
  const row = screen.container.querySelector('[aria-label="Ideas"] [role="option"]');
  expect(row?.querySelector('.vs-pickrow-bar')).toBeNull();
  expect(row?.querySelector('.vs-pickrow-figure')?.textContent).toBe('–');
  expect(row?.querySelector('.vs-chip[data-tone="err"]')?.textContent).toBe('Failed');
  expect(row?.querySelector('.vs-pickrow-text > .vs-text:last-child')?.textContent).not.toContain(
    'failed ·',
  );
  await screen.done();
});

test('sort control changes the flat order', async () => {
  const screen = await shown();
  const select = screen.container.querySelector('select') as HTMLSelectElement;
  expect(select.value).toBe('newest');
  await act(async () => {
    select.value = 'oldest';
    select.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
  });
  expect(titles(screen.container)).toEqual([
    'idea 0',
    'idea 1',
    'idea 2',
    'idea 3',
    'idea 4',
    'idea 5',
    'idea 6',
  ]);
  await screen.done();
});

function chips(
  container: HTMLElement,
): {label: string; pressed: string | null; disabled: boolean}[] {
  return [...container.querySelectorAll('button.vs-dotlegend-item')].map(each => ({
    label: each.textContent ?? '',
    pressed: each.getAttribute('aria-pressed'),
    disabled: (each as HTMLButtonElement).disabled,
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

test('a chip lists its own group, shows it pressed, and a chip with nothing behind it is disabled', async () => {
  const screen = await shown();
  expect(chips(screen.container)).toEqual([
    {label: '6 passed', pressed: 'false', disabled: false},
    {label: '0 running', pressed: 'false', disabled: true},
    {label: '1 failed', pressed: 'false', disabled: false},
  ]);
  await click(screen.container, '1 failed');
  expect(titles(screen.container)).toEqual(['idea 4']);
  expect(chips(screen.container).map(each => each.pressed)).toEqual(['false', 'false', 'true']);
  await click(screen.container, '6 passed');
  expect(titles(screen.container)).toHaveLength(6);
  await click(screen.container, '6 passed');
  expect(titles(screen.container)).toHaveLength(7);
  await screen.done();
});

test('a search that matches nothing says so instead of leaving the list blank', async () => {
  const screen = await shown();
  await screen.press({
    type: 'ideas-filter',
    filter: {query: 'zzzz', passed: true, running: true, failed: true},
  });
  expect(titles(screen.container)).toEqual([]);
  expect(screen.container.textContent).toContain('No ideas match “zzzz”');
  await screen.done();
});

test('a ratio against a base reads short in the column and whole on hover', async () => {
  const screen = await shown(true);
  const figures = [...screen.container.querySelectorAll('[aria-label="Ideas"] .vs-pickrow-figure')];
  const short = figures.find(each => (each.textContent ?? '').includes('1.10×'));
  expect(short?.textContent).toMatch(/^1\.10× (r\d+|base)$/);
  expect(short?.getAttribute('title')).toMatch(/^1\.10× vs /);
  expect(short?.getAttribute('title')).not.toBe(short?.textContent);
  // A plain ratio to the input is its own title.
  const plain = figures.find(each => each.textContent === '1.01×');
  expect(plain?.getAttribute('title')).toBe('1.01×');
  await screen.done();
});
