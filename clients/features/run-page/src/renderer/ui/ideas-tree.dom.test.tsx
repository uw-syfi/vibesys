/**
 * The Ideas face's lineage tree in a DOM: the best line replaces the old best-path row, the tree
 * is the default once ideas build on each other, a folded branch opens in place, and a search
 * keeps the ideas it finds with the ideas they build on.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act, useState} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {modelOfShape} from '../../state/ideas-tree-fixtures.js';
import {
  type RunPageAction,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from '../../state/index.js';
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

// 0 ─ 1 ─ 2 (best)
//  └─ 3 ─ 4 (failed)
//       └─ 5 ─ 6
const SHAPE = {
  parents: [null, 0, 1, 0, 3, 3, 5],
  outcomes: ['passed', 'passed', 'passed', 'passed', 'failed', 'passed', 'passed'],
  best: 2,
} as const;

function initial(): RunPageState {
  const state = runPageInitial({
    identity: {host: 'ssh:chelan4', runId: 'run-1'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'chelan4',
    unavailable: {},
  });
  return {...state, ideas: {...state.ideas, failed: true}};
}

interface Screen {
  readonly container: HTMLElement;
  readonly press: (action: RunPageAction) => Promise<void>;
  readonly done: () => Promise<void>;
}

async function shown(shape: typeof SHAPE | {parents: readonly (number | null)[]}): Promise<Screen> {
  const container = document.createElement('div');
  document.body.append(container);
  const root: Root = createRoot(container);
  const model = modelOfShape(shape);
  const base = {model, clock: {start: 0, now: null}, tokens: EMPTY_LEDGER, zoom: () => undefined};
  let send: (action: RunPageAction) => void = () => undefined;
  function Harness() {
    const [state, setState] = useState(initial());
    send = action => setState(now => runPageStep(now, action).state);
    return <RunLevel state={state} base={base} core={null} mode="ideas" dispatch={send} />;
  }
  await act(async () => root.render(<Harness />));
  return {
    container,
    press: async action => {
      await act(async () => send(action));
    },
    done: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

function titles(screen: Screen): string[] {
  return [
    ...screen.container.querySelectorAll('[role="option"] .vs-pickrow-text > .vs-text:first-child'),
  ].map(each => each.textContent ?? '');
}

function rowOf(screen: Screen, title: string): Element {
  const row = [...screen.container.querySelectorAll('[role="option"]')].find(
    each => each.querySelector('.vs-pickrow-text > .vs-text')?.textContent === title,
  );
  if (row === undefined) throw new Error(`no row ${title}`);
  return row;
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new window.MouseEvent('click', {bubbles: true}) as unknown as Event);
  });
}

test('the best line names the best idea and its steps; there is no best-path row', async () => {
  const screen = await shown(SHAPE);
  const text = screen.container.textContent ?? '';
  expect(text).toContain('best:');
  expect(text).toContain('3 steps from input');
  expect(text).not.toContain('best path');
  expect(screen.container.querySelector('button[title^="Show the best idea"]')?.textContent).toBe(
    'idea 2',
  );
  await screen.done();
});

test('the tree is the default once an idea builds on another; the switch goes to the list', async () => {
  const screen = await shown(SHAPE);
  const tree = screen.container.querySelector('input[value="tree"]') as HTMLInputElement;
  expect(tree.checked).toBe(true);
  // Newest first, the best chain open, the other branch folded to one row.
  expect(titles(screen)).toEqual(['idea 0', 'idea 3', 'idea 1', 'idea 2']);
  await screen.press({type: 'ideas-layout', layout: 'list'});
  expect((screen.container.querySelector('input[value="list"]') as HTMLInputElement).checked).toBe(
    true,
  );
  expect(titles(screen)).toEqual([
    'idea 6',
    'idea 5',
    'idea 4',
    'idea 3',
    'idea 2',
    'idea 1',
    'idea 0',
  ]);
  expect(screen.container.textContent).toContain('from');
  await screen.done();
});

test('a run whose ideas all build on the input starts as the list', async () => {
  const screen = await shown({parents: [null, null, null]});
  expect((screen.container.querySelector('input[value="list"]') as HTMLInputElement).checked).toBe(
    true,
  );
  expect(titles(screen)).toEqual(['idea 2', 'idea 1', 'idea 0']);
  await screen.done();
});

test('a folded branch says what it holds and opens in place, then folds again', async () => {
  const screen = await shown(SHAPE);
  const branch = rowOf(screen, 'idea 3');
  expect(branch.textContent).toContain('→ 3 ideas');
  expect(branch.textContent).toContain('1 failed');
  expect(branch.querySelector('.vs-pickrow-fold[data-fold="closed"]')).not.toBeNull();
  await click(branch.querySelector('.vs-pickrow-fold') as Element);
  expect(titles(screen)).toContain('idea 5');
  expect(titles(screen)).toContain('idea 4');
  expect(titles(screen)).not.toContain('idea 6');
  expect(rowOf(screen, 'idea 5').textContent).toContain('→ 1 idea');
  await click(
    rowOf(screen, 'idea 3').querySelector('.vs-pickrow-fold[data-fold="open"]') as Element,
  );
  expect(titles(screen)).not.toContain('idea 5');
  await screen.done();
});

test('a search keeps the ideas it finds and the ideas they build on, all open', async () => {
  const screen = await shown(SHAPE);
  await screen.press({
    type: 'ideas-filter',
    filter: {...initial().ideas, query: 'idea 6'},
  });
  expect(titles(screen)).toEqual(['idea 0', 'idea 3', 'idea 5', 'idea 6']);
  await screen.done();
});

test('pressing the best line selects the best idea', async () => {
  const screen = await shown(SHAPE);
  await click(screen.container.querySelector('button[title^="Show the best idea"]') as Element);
  expect(rowOf(screen, 'idea 2').getAttribute('aria-selected')).toBe('true');
  await screen.done();
});
