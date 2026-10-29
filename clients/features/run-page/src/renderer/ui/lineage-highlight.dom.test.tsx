/** The chart highlights an idea's path while the pointer is on its row or point. */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {EMPTY_LEDGER} from '@vibesys/run-state';
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
  const base = {
    model,
    clock: {start: 0, now: null},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: () => undefined,
  };
  function Harness() {
    const [state, setState] = useState(() =>
      runPageInitial({
        identity: {host: 'ssh:host-b', runId: 'run-1'},
        title: {project: 'queue-rs', task: null},
        hostLabel: 'host-b',
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

const ringed = (container: HTMLElement): number =>
  container.querySelectorAll('.vs-chart-lit').length;

function row(container: HTMLElement, title: string): Element {
  const found = [...container.querySelectorAll('[role="option"]')].find(
    each => each.querySelector('.vs-pickrow-text > .vs-text')?.textContent === title,
  );
  if (found === undefined) throw new Error(`no row ${title}`);
  return found;
}

test('the chart joins no dots, and rings none until the reader points at something', async () => {
  const container = await shown();
  expect(container.querySelectorAll('.vs-chart-edge')).toHaveLength(0);
  expect(container.querySelectorAll('.vs-chart svg path, path.vs-chart-step').length).toBe(1);
  expect(ringed(container)).toBe(0);
  expect(container.querySelector('[role="option"][data-path]')).toBeNull();
});

test('pointing at a flat idea row lights its chain on the chart, and leaving clears it', async () => {
  const container = await shown();
  await hover(row(container, 'idea 5'), true);
  expect(ringed(container)).toBe(4);
  expect(container.querySelector('[role="option"][data-path]')).toBeNull();
  await hover(row(container, 'idea 5'), false);
  expect(ringed(container)).toBe(0);
});

test('pointing at a point lights its chain the same way', async () => {
  const container = await shown();
  const point = container.querySelector('[role="button"][aria-label^="idea 1"]') as Element;
  await hover(point, true);
  expect(ringed(container)).toBe(2);
  await hover(point, false);
  expect(ringed(container)).toBe(0);
});
