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
import {FIXTURE_NAMES, fixtureNamed} from '@vibesys/run-fixtures';
import {Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';
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
import {createRunPageStore, type RunPagePorts, type RunPageStore} from '../index.js';
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

async function screenText(state: RunPageState, resume?: RunPageStore): Promise<string> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const model = createRunReader().read(state.run.trace, null);
  const render = () => {
    const current = resume?.getState() ?? state;
    root.render(
      <RunScreen
        state={current}
        model={model}
        whole={model}
        core={current.run.core}
        route={RUN_ROUTE}
        zoom={() => undefined}
        dispatch={action => resume?.dispatch(action)}
      />,
    );
  };
  const unsubscribe = resume?.subscribe(render);
  await act(async () => render());
  if (resume) {
    const button = [...container.querySelectorAll('button')].find(
      each => each.textContent === 'Resume',
    );
    if (!button) throw new Error('Resume required');
    const before = container.textContent;
    await act(async () => button.click());
    expect(container.textContent).not.toBe(before);
    expect(container.textContent).toContain('Waiting for the run to start');
    expect(
      [...container.querySelectorAll('button')]
        .find(each => each.textContent === 'Resume')
        ?.getAttribute('aria-disabled'),
    ).toBe('true');
  }
  const text = container.textContent ?? '';
  unsubscribe?.();
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
  expect(text).toContain('Resume');
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

test('a saved run whose record expired reads as ended on the timeline, never Live', async () => {
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
  };
  step({type: 'record', record: {kind: 'historical', status: 'completed'}});
  const summary = asked.find(request => request.kind === 'read-summary');
  if (summary?.kind !== 'read-summary') throw new Error('no summary asked');
  step({type: 'summary', token: summary.token, reply: {kind: 'unsupported'}, wallMs: 0});
  const nextRead = () => asked.find(request => request.kind === 'read-history');
  for (let request = nextRead(); request?.kind === 'read-history'; request = nextRead()) {
    step({type: 'history', token: request.token, reply: pageOf(request.from, 40, undefined)});
  }
  expect(state.history.kind).toBe('read');
  const text = await screenText(state);
  expect(text).toContain('Saved run');
  expect(text).not.toContain('Live');
});

test('property: every legacy ending renders the host-admitted Resume, regardless of compatibility flags', async () => {
  const loaded = replay(FIXTURE.events.length, undefined).at(-1) as RunPageState;
  await fc.assert(
    fc.asyncProperty(
      fc.constantFrom('completed', 'failed', 'stopped'),
      fc.boolean(),
      async (status, resumable) => {
        const run: HostRun = {
          ...ENDED,
          status,
          phase: status,
          end: {outcome: status === 'completed' ? 'adopted' : status, reason: '', resumable},
        };
        const state = runPageStep(loaded, {type: 'record', record: {kind: 'ended', run}}).state;
        const text = await screenText(state);
        expect(text).toContain('Resume');
        // The run's logs are its Logs tab and Run again is in More actions; the banner repeats
        // neither.
        expect(text).not.toContain('View logs');
        expect(text).not.toContain('Run again');
      },
    ),
    {numRuns: 12, seed: 3436, examples: [['completed', false]]},
  );
});

/** A recorded host that accepts Resume while its next listing has not arrived yet. */
function resumePorts(name: string): RunPagePorts {
  const fixture = fixtureNamed(name);
  const page: HistoryReply = {
    kind: 'page',
    events: fixture.events,
    next: (fixture.events.at(-1)?.sequence ?? 0) + 1,
    complete: true,
    bytes: 0,
  };
  return {
    attach: async () => null,
    release: () => {},
    attachReader: async () => ({kind: 'unsupported'}),
    openSession: () => {
      throw new Error('An ended run needs no live session');
    },
    readHistory: async () => page,
    readTrace: async () => page,
    readSummary: async () => ({kind: 'unsupported'}),
    readSpan: async () => ({kind: 'unsupported'}),
    resumeEnded: async () => {},
    openWindow: () => {},
    now: () => 0,
    archive: () => {},
    runAgain: () => {},
    logs: null,
  };
}

test('property: Resume visibly acts on every recorded fixture with every legacy ending and compatibility flag', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.constantFrom(...FIXTURE_NAMES),
      fc.constantFrom('completed', 'failed', 'stopped'),
      fc.boolean(),
      async (name, status, resumable) => {
        const clock = new FakeClock();
        const store = createRunPageStore(
          {
            identity: {host: 'local', runId: fixtureNamed(name).runId},
            title: {project: '/project', task: 'mpmc'},
            hostLabel: 'host-a',
            unavailable: {},
          },
          resumePorts(name),
          clock.schedule,
        );
        try {
          await act(async () =>
            store.dispatch({
              type: 'record',
              record: {
                kind: 'ended',
                run: {
                  ...ENDED,
                  status,
                  phase: status,
                  end: {
                    outcome: status === 'completed' ? 'adopted' : status,
                    reason: '',
                    resumable,
                  },
                },
              },
            }),
          );
          await screenText(store.getState(), store);
        } finally {
          store.dispose();
        }
        expect(clock.pending).toBe(0);
      },
    ),
    {
      numRuns: 16,
      seed: 34366,
      examples: FIXTURE_NAMES.map(name => [name, 'completed' as const, false]),
    },
  );
});
