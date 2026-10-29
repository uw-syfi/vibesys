/**
 * The Ideas list and the idea view over the live `bases-and-effects` run: each idea names what it
 * started from, the leading idea reads Best while the run works, the framework's effect verdict
 * sits beside passed and failed, and no row says its status twice or says "turned down".
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {EMPTY_LEDGER, initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act, type JSX} from 'react';
import {createRoot} from 'react-dom/client';
import {createRunReader, type RunModel, runPageInitial} from '../../state/index.js';
import {EMPTY_TRACE_LOG, traceStep} from '../../state/live.js';
import {IdeasWorkspace} from './levels.js';
import {RunLevel} from './run-level.js';
import type {ViewBase} from './views/props.js';

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

const EVENTS = fixtureNamed('bases-and-effects').events;
const MODEL: RunModel = createRunReader().read(
  traceStep(EMPTY_TRACE_LOG, {kind: 'events', events: EVENTS}),
  null,
);
const CORE = reduceEventBatch(initialCoreState(), EVENTS);

const STATE = runPageInitial({
  identity: {host: 'ssh:host-b', runId: 'run-1'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'host-b',
  unavailable: {},
});
const BASE: ViewBase = {
  model: MODEL,
  clock: {start: 0, now: null},
  tokens: EMPTY_LEDGER,
  who: new Map(),
  zoom: () => undefined,
};

async function rendered(
  element: JSX.Element,
): Promise<{container: HTMLElement; done: () => Promise<void>}> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(element));
  return {
    container,
    done: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

interface Line {
  readonly title: string;
  readonly line: string;
  readonly badge: string;
}

function lines(container: HTMLElement): Line[] {
  return [...container.querySelectorAll('[aria-label="Ideas"] [role="option"]')].map(row => ({
    title: row.querySelector('.vs-pickrow-text > .vs-text:first-child')?.textContent ?? '',
    line: row.querySelector('.vs-pickrow-text > .vs-text:nth-child(2)')?.textContent ?? '',
    badge: row.querySelector('.vs-chip')?.textContent ?? '',
  }));
}

test('the list says what each idea built on, marks the leader Best, and says each status once', async () => {
  const screen = await rendered(
    <RunLevel state={STATE} base={BASE} core={CORE} mode="ideas" dispatch={() => undefined} />,
  );
  const rows = lines(screen.container);
  const byTitle = (title: string) => rows.find(row => row.title === title);
  expect(byTitle('Padded counters')?.line).toBe('From input · beat its base');
  expect(byTitle('Batched enqueue on the padded counters')?.line).toBe('From best · beat its base');
  expect(byTitle('Per-core free lists')?.line).toBe('From a passed idea · did not beat its base');
  expect(byTitle('Prefetch the next slot')?.line).toBe('From its own work · beat its base');
  expect(rows.filter(row => row.badge === 'Best').map(row => row.title)).toEqual([
    'Prefetch the next slot',
  ]);
  expect(rows.some(row => row.badge === 'Adopted')).toBe(false);
  for (const row of rows) expect(row.line.toLowerCase()).not.toBe(row.badge.toLowerCase());
  expect(byTitle('Spin before parking')).toMatchObject({
    badge: 'Passed',
    line: 'From input · within noise of its base',
  });
  expect(byTitle('Widen the batch to sixty-four')).toMatchObject({
    badge: 'Failed',
    line: 'From best',
  });
  // Still taking in runs: no verdict yet, so never a provisional "within noise".
  expect(byTitle('Combine adjacent slots')?.line).toBe('From its own work · measuring');
  expect(screen.container.textContent).not.toMatch(/turned down/i);
  await screen.done();
});

test('an idea that measured nothing claims no verdict in the idea view, only its outcome', async () => {
  const idea = MODEL.candidates.candidates.find(
    item => item.title === 'Widen the batch to sixty-four',
  );
  if (idea === undefined) throw new Error('fixture needs the idea');
  const screen = await rendered(
    <IdeasWorkspace
      state={STATE}
      base={BASE}
      core={CORE}
      mode="ideas"
      idea={idea}
      loading={null}
      dispatch={() => undefined}
    />,
  );
  const pane = [...screen.container.querySelectorAll('.vs-pane')].at(-1)?.textContent ?? '';
  expect(pane).toContain('Failed');
  expect(pane).not.toMatch(/within noise|supported|disproven/);
  await screen.done();
});

test('the idea view says what the idea built on, its effect, and its outcome', async () => {
  const idea = MODEL.candidates.candidates.find(item => item.title === 'Spin before parking');
  if (idea === undefined) throw new Error('fixture needs the idea');
  const screen = await rendered(
    <IdeasWorkspace
      state={STATE}
      base={BASE}
      core={CORE}
      mode="ideas"
      idea={idea}
      loading={null}
      dispatch={() => undefined}
    />,
  );
  const pane = screen.container.textContent ?? '';
  expect(pane).toContain('Passed');
  expect(pane).toContain('within noise of its base');
  expect(pane).toContain('Passed · 1.07× the input');
  expect(pane).toContain('from input');
  expect(pane).not.toMatch(/turned down/i);
  await screen.done();
});
