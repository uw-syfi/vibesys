/**
 * The run level's faces in a DOM while a live run attaches: they say the run is being read until
 * its first snapshot is folded, and only then that it has no ideas or agents yet. Regression:
 * attaching to a live run flashed "No ideas yet" until the snapshot arrived.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {HostRun} from '@vibesys/hosts';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  createRunReader,
  type RunMode,
  type RunModel,
  type RunPageAction,
  type RunPageRequest,
  type RunPageState,
  runPageInitial,
  runPageStep,
  summaryFacts,
  type ZoomEvent,
} from '../../state/index.js';
import {idea, modelOf} from '../../state/timeline-fixtures.js';
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

const RUNNING: HostRun = {
  id: 'a1b2c3d4e5f6',
  projectRoot: '~/vibesys-projects/queue-rs',
  runId: 'run-1',
  status: 'running',
  phase: 'running',
  paused: null,
  end: null,
  startedAt: 1,
  reason: null,
};

/** A live run's page attached and waiting for its first snapshot. */
function attaching(): RunPageState {
  let state = runPageInitial({
    identity: {host: 'ssh:host-b', runId: 'run-1'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'host-b',
    unavailable: {},
  });
  const asked: RunPageRequest[] = [];
  const step = (action: RunPageAction): void => {
    const next = runPageStep(state, action);
    state = next.state;
    asked.push(...next.requests);
  };
  step({type: 'record', record: {kind: 'live', run: RUNNING}});
  const attach = asked.find(request => request.kind === 'attach');
  if (attach?.kind !== 'attach') throw new Error('no attach asked');
  step({type: 'attached', token: attach.token, error: null});
  return state;
}

const SNAPSHOT: RunPageAction = {
  type: 'session',
  session: {hasSnapshot: true, streaming: true, controls: true, error: null},
};

async function face(state: RunPageState, mode: RunMode): Promise<string> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const model = createRunReader().read(state.run.trace, null);
  const base = {
    model,
    clock: {start: 0, now: null},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: () => undefined,
  };
  await act(async () =>
    root.render(
      <RunLevel state={state} base={base} core={null} mode={mode} dispatch={() => undefined} />,
    ),
  );
  const text = container.textContent ?? '';
  await act(async () => root.unmount());
  container.remove();
  return text;
}

test('a face reads the run until the first snapshot, and only then says it is empty', async () => {
  const before = attaching();
  const after = runPageStep(before, SNAPSHOT).state;
  for (const [mode, empty] of [
    ['ideas', 'No ideas yet'],
    ['timeline', 'No ideas yet'],
    ['agents', 'No agents yet'],
  ] as const) {
    const waiting = await face(before, mode);
    expect(waiting).toContain('Reading the run…');
    expect(waiting).not.toContain(empty);
    const read = await face(after, mode);
    expect(read).toContain(empty);
    expect(read).not.toContain('Reading the run…');
  }
});

/** The run's ideas as measured at 100, 200 and 300 seconds, read to their live end or as of `scrub`. */
function measuredRun(scrub: number | null): RunModel {
  const all = [10, 20, 30].map((value, at) => {
    const item = idea(`i${at}`, 'passed', at * 100, 90);
    const measured = item.measurement;
    if (measured === null) throw new Error('fixture');
    return {...item, measurement: {...measured, at: (at + 1) * 100, metricValue: value}};
  });
  const seen = scrub === null ? all : all.filter(item => (item.measurement?.at ?? 0) <= scrub);
  return {
    ...modelOf(seen),
    at: scrub === null ? null : scrub + 1,
    latest: scrub,
    overview: null,
    inputGates: [],
    changed: new Set<string>(),
    agents: {agents: []},
    trace: {spans: new Map()},
  } as unknown as RunModel;
}

async function chartAt(scrub: number | null): Promise<{yLabels: string[]; cursor: boolean} | null> {
  const state = runPageStep(attaching(), SNAPSHOT).state;
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const model = measuredRun(scrub);
  const base = {
    model,
    clock: {start: 0, now: null},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: () => undefined,
  };
  await act(async () =>
    root.render(
      <>
        <Hero {...base} whole={measuredRun(null)} idea={null} />
        <RunLevel state={state} base={base} core={null} mode="ideas" dispatch={() => undefined} />
      </>,
    ),
  );
  const chart = container.querySelector('svg.vs-chart');
  const seen =
    chart === null
      ? null
      : {
          yLabels: [...chart.querySelectorAll('text.vs-chart-label')].map(
            each => each.textContent ?? '',
          ),
          cursor: chart.querySelector('.vs-chart-playhead') !== null,
        };
  await act(async () => root.unmount());
  container.remove();
  return seen;
}

test('regression: scrubbing keeps the chart drawn with the same axes, before and after every measurement', async () => {
  const live = await chartAt(null);
  expect(live).not.toBeNull();
  for (const scrub of [0, 50, 100, 150, 200, 250, 300]) {
    const seen = await chartAt(scrub);
    // Rendered at every position, even before anything was measured, with the whole run's labels.
    expect(seen?.yLabels).toEqual(live?.yLabels as string[]);
    expect(seen?.cursor).toBe(true);
  }
});

/** Three ideas: one passed (the best), one failed for a reason, one running; `adopted` once over. */
function namedRun(adopted: boolean): RunModel {
  const failed = {...idea('turned', 'failed', 100, 90), reason: 'turned_down'};
  const items = [idea('good', 'passed', 0, 90), failed, idea('going', 'running', 200, 90)];
  return {
    ...modelOf(items, {candidate: 'good', adopted}),
    overview: null,
    inputGates: [],
    changed: new Set<string>(),
    agents: {agents: []},
    trace: {spans: new Map()},
  } as unknown as RunModel;
}

async function textOf(model: RunModel, showFailed: boolean): Promise<string> {
  const state = runPageStep(attaching(), SNAPSHOT).state;
  const shown = showFailed ? state : {...state, ideas: {...state.ideas, failed: false}};
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const base = {
    model,
    clock: {start: 0, now: null},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: () => undefined,
  };
  await act(async () =>
    root.render(
      <>
        <Hero {...base} whole={model} idea={null} />
        <RunLevel state={shown} base={base} core={null} mode="ideas" dispatch={() => undefined} />
      </>,
    ),
  );
  const text = container.textContent ?? '';
  await act(async () => root.unmount());
  container.remove();
  return text;
}

test('the run level says passed, failed, best and adopted, never accepted, kept or rejected', async () => {
  for (const adopted of [false, true]) {
    for (const showFailed of [false, true]) {
      const text = await textOf(namedRun(adopted), showFailed);
      expect(text).toContain('1 passed');
      expect(text).toContain('1 failed');
      expect(text).toContain('1 running');
      expect(text).toContain(adopted ? 'Adopted' : 'Best so far');
      expect(text).not.toMatch(/turned down/i);
      expect(text).not.toMatch(/accepted|kept|rejected/i);
    }
  }
  expect(await textOf(namedRun(false), false)).not.toContain('adopted');
});

test('the performance row is one element whatever idea is open: dots are singled out and a dot opens its idea', async () => {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const model = measuredRun(null);
  const opened: ZoomEvent[] = [];
  const base = {
    model,
    clock: {start: 0, now: null},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: (event: ZoomEvent) => {
      opened.push(event);
    },
  };
  const draw = (idea: string | null) =>
    act(async () => root.render(<Hero {...base} whole={model} idea={idea} />));
  const kinds = () =>
    [...container.querySelectorAll('.vs-chart-pick')].map(
      point => point.getAttribute('data-emphasis') ?? '-',
    );
  const chartNode = () => container.querySelector('svg.vs-chart');

  await draw(null);
  const chart = chartNode();
  expect(chart).not.toBeNull();
  expect(kinds()).toEqual(['-', '-', '-']);
  for (const idea of ['i1', null, 'i0', 'ghost', 'i2']) {
    await draw(idea);
    // Never remounted: the very same node, every dot still drawn.
    expect(chartNode()).toBe(chart);
    expect(kinds()).toHaveLength(3);
    const focused = kinds().filter(kind => kind === 'focus').length;
    expect(focused).toBe(['i1', 'i0', 'i2'].includes(idea ?? '') ? 1 : 0);
  }
  await draw('i1');
  expect(kinds()).toEqual(['dim', 'focus', 'dim']);
  // The idea's own result reads under the hero's figure.
  expect(container.textContent).toContain('Selected idea:');

  const third = container.querySelectorAll('.vs-chart-pick')[2];
  await act(async () => {
    third?.dispatchEvent(new MouseEvent('click', {bubbles: true}));
  });
  expect(opened).toEqual([{type: 'open-idea', idea: 'i2'}]);
  await act(async () => root.unmount());
  container.remove();
});

/** A saved run whose journal predates the run trace, opened from its summary. */
function recordedBeforeTrace(): RunPageState {
  const initial = runPageInitial({
    identity: {host: 'ssh:host-b', runId: 'run-1'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'host-b',
    unavailable: {},
  });
  const asked = runPageStep(initial, {type: 'record', record: {kind: 'historical', status: null}});
  const request = asked.requests.find(each => each.kind === 'read-summary');
  if (request?.kind !== 'read-summary') throw new Error('no summary asked');
  const none = {input: null, cacheRead: null, cacheWrite: null, output: null, reasoning: null};
  const summary = {
    throughSequence: 10,
    eventCount: 10,
    journalBytes: 1,
    trace: 'predates_trace' as const,
    roundsFinished: 12,
    overview: null,
    spans: [],
    replies: [],
    tokens: none,
    usage: [],
    records: [],
    inputGates: [],
    terminal: null,
  };
  return runPageStep(asked.state, {
    type: 'summary',
    token: request.token,
    reply: {kind: 'summary', ...summaryFacts(summary, 'run-1', 'stopped')},
    wallMs: 0,
  }).state;
}

test('regression: a run recorded before the trace says so on every face, never "No ideas yet"', async () => {
  const state = recordedBeforeTrace();
  for (const mode of ['ideas', 'timeline', 'agents'] as const) {
    const read = await face(state, mode);
    expect(read).toContain('Recorded by an older VibeSys');
    expect(read).toContain('12 rounds');
    expect(read).not.toContain('No ideas yet');
    expect(read).not.toContain('No agents yet');
    expect(read).not.toContain('Reading the run…');
  }
});

test('regression: a saved run’s Logs face ends in a notice, not in "Looking for the run’s logs…"', async () => {
  const read = await face(recordedBeforeTrace(), 'logs');
  expect(read).toContain('logs are not available');
  expect(read).not.toContain('Looking for');
});
