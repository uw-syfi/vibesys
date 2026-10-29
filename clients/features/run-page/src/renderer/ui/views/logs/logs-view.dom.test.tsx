/**
 * The Logs face through its props, in a browser DOM: each scope shows only its own streams, the
 * chosen stream's lines fill the pane (windowed, however long), and the face asks the page to
 * follow the stream it shows.
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun} from '@vibesys/hosts';
import {emptyLogView, type LogView} from '@vibesys/logs';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import type {Candidate, CandidateAttempt, CandidateStep} from '@vibesys/run-state/candidates';
import {Seeded} from '@vibesys/store/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  createRunReader,
  EMPTY_TRACE_LOG,
  type RunPageState,
  runPageInitial,
} from '../../../../state/index.js';
import {streamToFollow} from '../../../../state/scoped-logs.js';
import {type LogScope, LogsView, logsHeader} from './logs-view.js';

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

const MODEL = createRunReader().read(EMPTY_TRACE_LOG, null);
const CLOCK = {start: 0, now: 10};

function step(span: string): CandidateStep {
  return {span, kind: 'gate', title: span, role: null, status: 'done', startedAt: 0, endedAt: 1};
}

function attempt(span: string, steps: readonly string[]): CandidateAttempt {
  return {
    span,
    title: span,
    idea: null,
    round: null,
    outcome: 'running',
    reason: null,
    revision: null,
    baseRound: null,
    startedAt: 0,
    endedAt: null,
    measurement: null,
    steps: steps.map(step),
  };
}

function idea(id: string, attempts: readonly CandidateAttempt[]): Candidate {
  return {
    id,
    hypothesis: null,
    parent: null,
    title: id,
    idea: null,
    round: null,
    outcome: 'running',
    reason: null,
    baseRound: null,
    measurement: null,
    attempts,
    startedAt: 0,
    endedAt: null,
    running: [],
  };
}

/** A gate stage stream written for `span` (null: a writer that did not know its span). */
function gate(id: string, span: string | null, closed = true): LogStream {
  return {
    id,
    owner: {
      kind: 'gate_stage',
      gate: 'build',
      execution_id: 'e1',
      attempt: Number(id.slice(1)),
      ...(span === null ? {} : {span_id: span}),
    },
    command: 'cmd',
    retained_start: 0,
    size: 0,
    outcome: closed ? 'ended' : null,
  };
}

function viewOf(lines: readonly string[]): LogView {
  return {
    ...emptyLogView({maxLines: 100_000, maxLineChars: 4000}),
    lines: lines.map(text => ({text, runs: [{length: text.length, pipe: 'stdout'}]})),
    closed: true,
    eof: true,
  };
}

function pageState(
  streams: readonly LogStream[],
  views: Readonly<Record<string, LogView>> = {},
): RunPageState {
  const run: HostRun = {
    id: 'a1b2c3d4e5f6',
    projectRoot: '~/queue-rs',
    runId: 'run',
    status: 'running',
    phase: 'running',
    paused: null,
    end: null,
    startedAt: 1,
    reason: null,
  };
  return {
    ...runPageInitial({
      identity: {host: 'ssh:host-b', runId: 'run'},
      title: {project: 'queue-rs', task: 'mpmc'},
      hostLabel: 'host-b',
      unavailable: {},
    }),
    record: {kind: 'live', run},
    logs: {kind: 'listed', instanceId: run.id, streams},
    views,
  };
}

interface Shown {
  /** The stream ids listed beside the pane (empty when the pane stands alone). */
  readonly rows: readonly string[];
  readonly lines: readonly string[];
  readonly text: string;
  readonly selects: readonly string[];
}

/** The id a gate stream's row is titled with: `gate()` makes its attempt the number in its id. */
function idOfRow(title: string): string {
  const attempt = /attempt (\d+)/.exec(title)?.[1];
  return attempt === undefined ? title : `s${attempt}`;
}

async function show(state: RunPageState, scope: LogScope, click?: string): Promise<Shown> {
  const container = document.createElement('div');
  document.body.append(container);
  const selects: string[] = [];
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <LogsView
        model={MODEL}
        clock={CLOCK}
        tokens={EMPTY_LEDGER}
        who={new Map()}
        zoom={() => undefined}
        state={state}
        scope={scope}
        dispatch={action => {
          if (action.type === 'select-stream') selects.push(action.streamId);
        }}
      />,
    ),
  );
  if (click !== undefined) {
    const row = [...container.querySelectorAll<HTMLElement>('[role=option]')].find(
      node =>
        idOfRow(node.querySelector('.vs-pickrow-text > :first-child')?.textContent ?? '') === click,
    );
    await act(async () => row?.click());
  }
  const texts = (selector: string): string[] =>
    [...container.querySelectorAll(selector)].map(node => node.textContent ?? '');
  const shown = {
    rows: [...container.querySelectorAll('[role=option] .vs-pickrow-text > :first-child')].map(
      node => idOfRow(node.textContent ?? ''),
    ),
    lines: texts('.vs-log-line'),
    text: container.textContent ?? '',
    selects,
  };
  await act(async () => root.unmount());
  container.remove();
  return shown;
}

test('an idea lists the streams of its attempts and steps, a step only its own, the run all', async () => {
  const random = new Seeded(7);
  const spans = ['a1', 'a1/g1', 'a1/g2', 'a2', 'a2/g1', 'other'];
  const mine = new Set(['a1', 'a1/g1', 'a1/g2']);
  for (let round = 0; round < 25; round++) {
    const streams = Array.from({length: 8}, (_, index) =>
      gate(`s${index}`, random.bool() ? random.pick(spans) : null),
    );
    const state = pageState(streams);
    const ids = (keep: (span: string) => boolean): string[] =>
      streams.filter(stream => keep(String(stream.owner['span_id']))).map(stream => stream.id);

    const ofIdea = ids(span => mine.has(span));
    const ideaScope: LogScope = {
      kind: 'idea',
      idea: idea('a1', [attempt('a1', ['a1/g1', 'a1/g2'])]),
    };
    const stepScope: LogScope = {kind: 'step', step: step('a2/g1')};
    const shownIdea = await show(state, ideaScope);
    // One stream stands alone as a pane; none is said in words.
    expect(shownIdea.rows).toEqual(ofIdea.length > 1 ? ofIdea : []);
    expect(streamToFollow(state, ideaScope)).toBe(ofIdea.at(-1) ?? null);

    const ofStep = ids(span => span === 'a2/g1');
    const shownStep = await show(state, stepScope);
    expect(shownStep.rows).toEqual(ofStep.length > 1 ? ofStep : []);
    expect(streamToFollow(state, stepScope)).toBe(ofStep.at(-1) ?? null);
    if (ofStep.length === 0) expect(shownStep.text).toContain('This step wrote no log.');

    const shownRun = await show(state, {kind: 'run'});
    expect(shownRun.rows).toEqual(streams.map(stream => stream.id));
  }
});

test('fifty thousand lines are windowed; a finished log opens at its top, a live one at its tail', async () => {
  const lines = Array.from({length: 50_000}, (_, index) => `line ${index}`);
  const finished = await show(pageState([gate('s0', null)], {s0: viewOf(lines)}), {kind: 'run'});
  expect(finished.lines.length).toBeLessThan(1000);
  expect(finished.lines.at(0)).toBe('line 0');
  const live = await show(pageState([gate('s0', null, false)], {s0: viewOf(lines)}), {kind: 'run'});
  expect(live.lines.length).toBeLessThan(1000);
  expect(live.lines.at(-1)).toBe('line 49999');
});

test('the run lists its streams with their last line, and choosing one selects it', async () => {
  const state = pageState([gate('s0', null), gate('s1', null)], {s0: viewOf(['a', 'last words'])});
  const shown = await show(state, {kind: 'run'}, 's1');
  expect(shown.rows).toEqual(['s0', 's1']);
  expect(shown.text).toContain('last words');
  expect(shown.selects).toEqual(['s1']);
  expect(streamToFollow(state, {kind: 'run'})).toBe('s0');
});

test('a step’s Logs pane keeps the kicker of its step pane, so the role is said once', () => {
  const turn = {...step('t1'), kind: 'agent_turn', role: 'implementer', title: 'implementer'};
  const props = {
    model: MODEL,
    clock: CLOCK,
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: () => undefined,
    state: pageState([]),
    scope: {kind: 'step', step: turn} as const,
    dispatch: () => undefined,
  };
  expect(logsHeader(props)).toEqual({kicker: 'Agent turn'});
  expect(logsHeader({...props, scope: {kind: 'run'}})).toEqual({});
});
