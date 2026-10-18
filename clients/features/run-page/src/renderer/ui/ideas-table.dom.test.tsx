import {afterAll, beforeAll, expect, test} from 'bun:test';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act, useState} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {modelOfShape} from '../../state/ideas-tree-fixtures.js';
import {type RunPageAction, runPageInitial, runPageStep} from '../../state/index.js';
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
  const fixture = modelOfShape(SHAPE);
  const model = {
    ...fixture,
    candidates: {...fixture.candidates, best: {candidate: 'n2', adopted: true}},
  };
  const base = {model, clock: {start: 0, now: null}, tokens: EMPTY_LEDGER, zoom: () => undefined};
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
  ).toHaveLength(1);
  expect(
    screen.container.querySelector('[aria-label="Ideas"] .vs-chip[data-tone="ok"]')?.textContent,
  ).toBe('Adopted');
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
