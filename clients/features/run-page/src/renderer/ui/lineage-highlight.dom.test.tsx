/**
 * The lineage lit across the Run screen's performance row and the ideas tree: connectors on the
 * chart (the best chain emphasized by default), the chain of the idea the pointer is on or the
 * best line pinned lit on both, and nothing moved by it.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {modelOfShape} from '../../state/ideas-tree-fixtures.js';
import {ideaAncestry, type RunPageAction, runPageInitial, runPageStep} from '../../state/index.js';
import {Hero} from './hero.js';
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
//  └─ 3 ─ 4 ─ 5
const SHAPE = {
  parents: [null, 0, 1, 0, 3, 4],
  outcomes: ['passed', 'passed', 'passed', 'passed', 'passed', 'passed'],
  best: 2,
} as const;

async function shown() {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const model = modelOfShape(SHAPE);
  const base = {model, clock: {start: 0, now: null}, zoom: () => undefined};
  function Harness() {
    const [state, setState] = useState(() =>
      runPageInitial({
        identity: {host: 'ssh:chelan4', runId: 'run-1'},
        title: {project: 'queue-rs', task: null},
        hostLabel: 'chelan4',
        unavailable: {},
      }),
    );
    const dispatch = (action: RunPageAction): void =>
      setState(now => runPageStep(now, action).state);
    const lit = ideaAncestry(model, state.ideasHovered ?? state.ideasPinned);
    return (
      <>
        <Hero
          {...base}
          whole={model}
          idea={null}
          lit={lit}
          hover={idea => dispatch({type: 'ideas-hover', idea})}
        />
        <RunLevel state={state} base={base} core={null} mode="ideas" dispatch={dispatch} />
      </>
    );
  }
  await act(async () => root.render(<Harness />));
  return container;
}

async function hover(element: Element, on: boolean): Promise<void> {
  await act(async () => {
    element.dispatchEvent(
      new window.MouseEvent(on ? 'mouseover' : 'mouseout', {bubbles: true}) as unknown as Event,
    );
  });
}

const edges = (container: HTMLElement, emphasis: string): number =>
  container.querySelectorAll(`.vs-chart-edge[data-emphasis="${emphasis}"]`).length;

function row(container: HTMLElement, title: string): Element {
  const found = [...container.querySelectorAll('[role="option"]')].find(
    each => each.querySelector('.vs-pickrow-text > .vs-text')?.textContent === title,
  );
  if (found === undefined) throw new Error(`no row ${title}`);
  return found;
}

const litRows = (container: HTMLElement): string[] =>
  [...container.querySelectorAll('[role="option"][data-path]')].map(
    each => each.querySelector('.vs-pickrow-text > .vs-text')?.textContent ?? '',
  );

test('the chart draws a connector for each passed idea, the best chain emphasized', async () => {
  const container = await shown();
  expect(container.querySelectorAll('.vs-chart-edge')).toHaveLength(6);
  // 0, 1 and 2 are the best chain; nothing is lit until the reader points at something.
  expect(edges(container, 'strong')).toBe(3);
  expect(edges(container, 'lit')).toBe(0);
  expect(litRows(container)).toEqual([]);
});

test('pointing at a row lights its chain on the chart and in the tree, and leaving puts it back', async () => {
  const container = await shown();
  // Open the other branch so its rows are shown, then point at idea 5 (0, 3, 4, 5).
  await act(async () => {
    (container.querySelector('.vs-pickrow-fold[data-fold="closed"]') as HTMLElement).click();
  });
  await hover(row(container, 'idea 5'), true);
  expect(edges(container, 'lit')).toBe(4);
  expect(litRows(container).sort()).toEqual(['idea 0', 'idea 3', 'idea 4', 'idea 5']);
  await hover(row(container, 'idea 5'), false);
  expect(edges(container, 'lit')).toBe(0);
  expect(litRows(container)).toEqual([]);
});

test('pointing at a point lights its chain the same way', async () => {
  const container = await shown();
  const point = container.querySelector('[role="button"][aria-label^="idea 1"]') as Element;
  await hover(point, true);
  expect(edges(container, 'lit')).toBe(2);
  expect(litRows(container).sort()).toEqual(['idea 0', 'idea 1']);
  await hover(point, false);
  expect(edges(container, 'lit')).toBe(0);
});

test('the best line pins the best idea: its chain stays lit, and nothing on the chart moves', async () => {
  const container = await shown();
  const before = [...container.querySelectorAll('.vs-chart-point')].map(each => [
    each.getAttribute('cx'),
    each.getAttribute('cy'),
  ]);
  await act(async () => {
    (container.querySelector('button[title^="Show the best idea"]') as HTMLElement).click();
  });
  expect(edges(container, 'lit')).toBe(3);
  expect(litRows(container).sort()).toEqual(['idea 0', 'idea 1', 'idea 2']);
  const after = [...container.querySelectorAll('.vs-chart-point')].map(each => [
    each.getAttribute('cx'),
    each.getAttribute('cy'),
  ]);
  expect(after).toEqual(before);
});
