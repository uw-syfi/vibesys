/**
 * An ended run opened from the host reads its recorded events page by page. Until the last page
 * the fold is a prefix of the run, whose open spans are not running now and whose measurements are
 * not the only ones. Regression: the Run screen drew the prefix as the run, so a finished run
 * showed "running" counts, a Now pane and partial outcomes for as long as the read took, and
 * changed under the reader's hands when the last page landed.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import type {HostRun} from '@vibesys/hosts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {Seeded} from '@vibesys/store/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  createRunReader,
  type HistoryReply,
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

const FIXTURE = fixtureNamed('finished-run');

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

/** The page of `size` events from `from`, as the helper serves it; `total` when the host says. */
function pageOf(
  from: number,
  size: number,
  total: number | undefined,
): Extract<HistoryReply, {kind: 'page'}> {
  const events: readonly RunEvent[] = FIXTURE.events.filter(event => (event.sequence ?? 0) >= from);
  const page = events.slice(0, size);
  const next = (page.at(-1)?.sequence ?? from - 1) + 1;
  return {
    kind: 'page',
    events: page,
    next,
    complete: next > (FIXTURE.events.at(-1)?.sequence ?? 0),
    bytes: JSON.stringify(page).length,
    ...(total === undefined ? {} : {totalEvents: total}),
  };
}

/** Every state of an ended run's page as its history is read in pages of `size` events. */
function replay(size: number, total: number | undefined): readonly RunPageState[] {
  const states: RunPageState[] = [];
  let state = runPageInitial({
    identity: {host: 'ssh:remote-host', runId: FIXTURE.runId},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'remote-host',
    unavailable: {},
  });
  let asked: readonly RunPageRequest[] = [];
  const step = (action: RunPageAction): void => {
    const next = runPageStep(state, action);
    state = next.state;
    asked = next.requests;
    states.push(state);
  };
  step({type: 'record', record: {kind: 'ended', run: ENDED}});
  const summary = asked.find(request => request.kind === 'read-summary');
  if (summary?.kind !== 'read-summary') throw new Error('no summary asked');
  // The host serves no summary, so the history is what the screen waits for.
  step({type: 'summary', token: summary.token, reply: {kind: 'unsupported'}, wallMs: 0});
  states.splice(0, states.length - 1); // the screens from here on are the history's loading
  const nextRead = () => asked.find(request => request.kind === 'read-history');
  for (let request = nextRead(); request?.kind === 'read-history'; request = nextRead()) {
    step({type: 'history', token: request.token, reply: pageOf(request.from, size, total)});
  }
  return states;
}

async function screenText(state: RunPageState): Promise<string> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const model = createRunReader().read(state.run.trace, null);
  await act(async () =>
    root.render(
      <RunScreen
        state={state}
        model={model}
        whole={model}
        core={state.run.core}
        route={RUN_ROUTE}
        zoom={() => undefined}
        dispatch={() => undefined}
      />,
    ),
  );
  const text = container.textContent ?? '';
  await act(async () => root.unmount());
  container.remove();
  return text;
}

const MISLEADING = /running|\bnow\b/i;

test('no page of an ended run’s history draws a running count or a Now pane before the last', async () => {
  const seeded = new Seeded(7);
  const sizes = [1, 2, 5, 40, ...Array.from({length: 3}, () => 1 + seeded.int(30))];
  for (const size of sizes) {
    for (const total of [undefined, FIXTURE.events.length]) {
      const states = replay(size, total);
      const last = states.length - 1;
      expect(states[last]?.history.kind).toBe('read');
      // Sample a long replay at a stride, and every state near its end.
      const stride = Math.max(1, Math.floor(last / 12));
      for (let index = 0; index < last; index += last - index <= 4 ? 1 : stride) {
        const text = await screenText(states[index] as RunPageState);
        expect(text, `size ${size}, after ${index} of ${last} steps`).not.toMatch(MISLEADING);
        expect(text).toContain('Reading the run’s history…');
      }
    }
  }
});

test('the same prefixes read as a run in progress when drawn as the run: the test sees the defect', async () => {
  const prefixes = replay(5, undefined).slice(1, -1);
  let drawnRunning = false;
  for (const state of prefixes) {
    const text = await screenText({...state, history: {kind: 'read'}});
    drawnRunning ||= /running/i.test(text);
  }
  expect(drawnRunning).toBe(true);
});

test('the last page settles the screen: the run, final, with no loading left', async () => {
  const text = await screenText(replay(5, FIXTURE.events.length).at(-1) as RunPageState);
  expect(text).not.toContain('Reading the run’s history…');
  expect(text).not.toMatch(/[1-9]\d* running/);
  expect(text).not.toContain('Adopted the candidate');
  expect(text).not.toContain('View logs');
});

test('the loading screen counts events of the total once the host says it, else events so far', async () => {
  const total = FIXTURE.events.length;
  const reading = (known: number | undefined) =>
    replay(5, known).filter(state => state.history.kind === 'reading')[1] as RunPageState;
  expect(await screenText(reading(total))).toContain(`5 of ${total} events`);
  const open = await screenText(reading(undefined));
  expect(open).toMatch(/5 events · .* read so far/);
  expect(open).not.toContain(' of ');
});

test('an unresolved run shows loading progress without a false empty metric or ideas pane', async () => {
  const initial = runPageInitial({
    identity: {host: 'ssh:remote-host', runId: FIXTURE.runId},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'remote-host',
    unavailable: {},
  });
  const locating = await screenText(initial);
  expect(locating).toContain('Finding the run');
  expect(locating).not.toContain('No ideas yet');
  expect(locating).not.toContain('nothing measured yet');
  const opened = runPageStep(initial, {type: 'record', record: {kind: 'historical', status: null}});
  const summary = await screenText(opened.state);
  expect(summary).toContain('Loading the run summary');
  expect(summary).not.toContain('No ideas yet');
});
