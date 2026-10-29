/**
 * The metric chart of an ended run is the whole run's: scrubbing moves its cursor, never its axis.
 * Regression: scrubbed to the first position, the viewed trace is empty, so the run clock's start
 * fell back to 0 while the chart's times were epoch seconds; the time axis read "166666h 40m".
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {HostRun} from '@vibesys/hosts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {Seeded} from '@vibesys/store/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  createRunReader,
  RUN_ROUTE,
  type RunPageAction,
  type RunPageRequest,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from '../../state/index.js';
import {RunScreen} from './run-screen.js';

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

/** A run that began at a real date (epoch seconds), as a host's runs do. */
const EPOCH_START = Date.UTC(2026, 9, 17, 6, 0, 0) / 1000;

/** `value` with every numeric `*_at` time moved to the run's real start. */
function shifted<T>(value: T): T {
  if (Array.isArray(value)) return value.map(shifted) as T;
  if (typeof value !== 'object' || value === null) return value;
  return Object.fromEntries(
    Object.entries(value).map(([key, each]) => [
      key,
      key.endsWith('_at') && typeof each === 'number' ? each + EPOCH_START : shifted(each),
    ]),
  ) as T;
}

const FIXTURE = (() => {
  const named = fixtureNamed('finished-run');
  return {...named, events: shifted(named.events)};
})();

const ENDED: HostRun = {
  id: 'a1b2c3d4e5f6',
  projectRoot: '~/vibesys-projects/queue-rs',
  runId: FIXTURE.runId,
  status: 'completed',
  phase: 'completed',
  paused: null,
  end: {
    outcome: FIXTURE.end?.outcome ?? 'adopted',
    reason: FIXTURE.end?.reason ?? '',
    resumable: false,
  },
  startedAt: 1,
  reason: null,
};

/** The ended run with its whole history read, as the page holds it. */
function readRun(): RunPageState {
  let state = runPageInitial({
    identity: {host: 'ssh:host-a', runId: FIXTURE.runId},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'host-a',
    unavailable: {},
  });
  let asked: readonly RunPageRequest[] = [];
  const step = (action: RunPageAction): void => {
    const next = runPageStep(state, action);
    state = next.state;
    asked = next.requests;
  };
  step({type: 'record', record: {kind: 'ended', run: ENDED}});
  const summary = asked.find(request => request.kind === 'read-summary');
  if (summary?.kind !== 'read-summary') throw new Error('no summary asked');
  step({type: 'summary', token: summary.token, reply: {kind: 'unsupported'}, wallMs: 0});
  const nextRead = () => asked.find(request => request.kind === 'read-history');
  for (let request = nextRead(); request?.kind === 'read-history'; request = nextRead()) {
    const events = FIXTURE.events.filter(event => (event.sequence ?? 0) >= request.from);
    step({
      type: 'history',
      token: request.token,
      reply: {
        kind: 'page',
        events,
        next: (events.at(-1)?.sequence ?? request.from) + 1,
        complete: true,
        bytes: 0,
      },
    });
  }
  return state;
}

const UNIT_SECONDS: Readonly<Record<string, number>> = {h: 3600, m: 60, s: 1};

/** Seconds a duration label such as `1h 20m` or `0s` names; null for any other text. */
function secondsOf(label: string): number | null {
  const parts = label.trim().split(/\s+/);
  let total = 0;
  for (const part of parts) {
    const match = /^(\d+)([hms])$/.exec(part);
    if (match === null) return null;
    total += Number(match[1]) * (UNIT_SECONDS[match[2] as string] as number);
  }
  return total;
}

/** The labels under the metric chart's time axis with the run scrubbed to `at` (null: the end). */
async function axisAt(state: RunPageState, at: number | null): Promise<readonly string[]> {
  const reader = createRunReader();
  const model = reader.read(state.run.trace, at);
  const whole = reader.read(state.run.trace, null);
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <RunScreen
        state={{...state, view: at === null ? {at: 'live'} : {at}}}
        model={model}
        whole={whole}
        core={state.run.core}
        route={RUN_ROUTE}
        zoom={() => undefined}
        dispatch={() => undefined}
      />,
    ),
  );
  const labels = [...container.querySelectorAll('.vs-chart-label')].map(
    node => node.textContent ?? '',
  );
  await act(async () => root.unmount());
  container.remove();
  return labels.filter(label => secondsOf(label) !== null);
}

test('scrubbed to the first position, the metric chart keeps the whole run’s time axis', async () => {
  const state = readRun();
  const end = await axisAt(state, null);
  expect(end.length).toBeGreaterThan(1);
  expect(await axisAt(state, 0)).toEqual(end);
});

test('at every position of an ended run the time axis is the run’s own: finite, rising, within its span', async () => {
  const state = readRun();
  const sequences = FIXTURE.events.map(event => event.sequence ?? 0);
  const last = sequences.at(-1) ?? 0;
  const whole = createRunReader().read(state.run.trace, null);
  const end = await axisAt(state, null);
  const random = new Seeded(11);
  const positions = [0, 1, last, ...Array.from({length: 10}, () => random.int(last + 1))];
  const span =
    (whole.latest ?? 0) - Math.min(...[...whole.trace.spans.values()].map(s => s.started_at));
  for (const at of positions) {
    const seconds = (await axisAt(state, at)).map(label => secondsOf(label) as number);
    expect(seconds, `position ${at}`).toEqual(end.map(label => secondsOf(label) as number));
    expect(seconds[0]).toBe(0);
    expect(
      seconds.every((value, index) => index === 0 || value > (seconds[index - 1] as number)),
    ).toBe(true);
    expect(
      seconds.every(value => Number.isFinite(value) && value >= 0 && value <= span * 1.0001),
    ).toBe(true);
  }
});
