import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import type {HostRun} from '@vibesys/hosts';
import {
  type HistoryReply,
  type RunPageAction,
  type RunPageRequest,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from './run-page.js';
import {historyLoading, historyProblem, traceArrival} from './views.js';

/**
 * Whether the run's spans have reached the window, as the page's actions arrive: awaited while a
 * live run attaches and waits for its first snapshot, or an ended run's history is read; arrived
 * once any is folded; none when nothing will come. Regression: attaching to a live run showed "No
 * ideas yet" until the snapshot arrived.
 */

function server(phase: HostRun['phase'], end: HostRun['end'] = null): HostRun {
  return {
    id: 'a1b2c3d4e5f6',
    projectRoot: '~/vibesys-projects/queue-rs',
    runId: 'run-1',
    status: phase,
    phase,
    paused: null,
    end,
    startedAt: 1,
    reason: null,
  };
}

const LIVE: RunPageAction = {type: 'record', record: {kind: 'live', run: server('running')}};
const ENDED: RunPageAction = {
  type: 'record',
  record: {
    kind: 'ended',
    run: server('stopped', {outcome: 'stopped', reason: 'Stopped by you.', resumable: false}),
  },
};

function initial(): RunPageState {
  return runPageInitial({
    identity: {host: 'ssh:host-b', runId: 'run-1'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'host-b',
    unavailable: {},
  });
}

/** Folds `actions`; a function action is built from the requests asked so far. */
function fold(
  actions: readonly (RunPageAction | ((asked: readonly RunPageRequest[]) => RunPageAction))[],
): RunPageState {
  const asked: RunPageRequest[] = [];
  let state = initial();
  for (const each of actions) {
    const step = runPageStep(state, typeof each === 'function' ? each(asked) : each);
    state = step.state;
    asked.push(...step.requests);
  }
  return state;
}

function tokenOf(
  asked: readonly RunPageRequest[],
  kind: 'attach' | 'read-history' | 'read-summary',
): number {
  const request = asked.filter(each => each.kind === kind).at(-1);
  if (request === undefined || !('token' in request)) throw new Error(`no ${kind} asked`);
  return request.token;
}

const attached = (error: string | null) => (asked: readonly RunPageRequest[]) =>
  ({type: 'attached', token: tokenOf(asked, 'attach'), error}) as RunPageAction;

const session = (hasSnapshot: boolean): RunPageAction => ({
  type: 'session',
  session: {hasSnapshot, streaming: true, controls: true, error: null},
});

/** The host answered the summary read: it serves none, so the history is read instead. */
const NO_SUMMARY = (asked: readonly RunPageRequest[]) =>
  ({
    type: 'summary',
    token: tokenOf(asked, 'read-summary'),
    reply: {kind: 'unsupported'},
    wallMs: 0,
  }) as RunPageAction;

const history = (reply: HistoryReply) => (asked: readonly RunPageRequest[]) =>
  ({type: 'history', token: tokenOf(asked, 'read-history'), reply}) as RunPageAction;

const SPAN = {
  sequence: 1,
  type: 'span_started',
  timestamp: '2026-10-15T16:30:51Z',
  data: {kind: 'span_started', span: {id: 'a', parent: null, type: 'flow', name: 'a'}},
} as unknown as RunEvent;

describe('a live run', () => {
  test('awaits its spans while it attaches and until its first snapshot', () => {
    expect(traceArrival(fold([LIVE]))).toBe('awaited');
    expect(traceArrival(fold([LIVE, attached(null)]))).toBe('awaited');
    expect(traceArrival(fold([LIVE, attached(null), session(false)]))).toBe('awaited');
  });

  test('has them once a snapshot is folded, even one with no spans', () => {
    expect(traceArrival(fold([LIVE, attached(null), session(true)]))).toBe('arrived');
  });

  test('has them once a span event is folded', () => {
    const data: RunPageAction = {
      type: 'run-data',
      wallMs: 0,
      data: {kind: 'events', events: [SPAN]},
    };
    expect(traceArrival(fold([LIVE, attached(null), data]))).toBe('arrived');
  });

  test('awaits nothing once the attach was refused', () => {
    expect(traceArrival(fold([LIVE, attached('no server')]))).toBe('none');
  });
});

describe('an ended run', () => {
  test('keeps a folded live trace visible after the run ends while its summary request is pending', () => {
    const opened = runPageStep(initial(), LIVE);
    const summary = opened.requests.find(request => request.kind === 'read-summary');
    const attach = opened.requests.find(request => request.kind === 'attach');
    if (summary?.kind !== 'read-summary' || attach?.kind !== 'attach') {
      throw new Error('live run did not ask for summary and attach');
    }
    const linked = runPageStep(opened.state, {type: 'attached', token: attach.token, error: null});
    const folded = runPageStep(linked.state, {
      type: 'run-data',
      wallMs: 0,
      data: {kind: 'events', events: [SPAN]},
    });
    expect(traceArrival(folded.state)).toBe('arrived');

    const ended = runPageStep(folded.state, ENDED);
    expect(ended.state.history.kind).toBe('read');
    expect(ended.state.summary.kind).toBe('superseded');
    expect(traceArrival(ended.state)).toBe('arrived');
    expect(historyLoading(ended.state)).toBeNull();

    const late = runPageStep(ended.state, {
      type: 'summary',
      token: summary.token,
      reply: {kind: 'unsupported'},
      wallMs: 0,
    });
    expect(late.state).toBe(ended.state);
    expect(traceArrival(late.state)).toBe('arrived');
  });

  test('awaits its spans while its history is read', () => {
    expect(traceArrival(fold([ENDED]))).toBe('awaited');
  });

  test('awaits its spans while its history is read, from a host that serves no summary', () => {
    expect(traceArrival(fold([ENDED, NO_SUMMARY]))).toBe('awaited');
  });

  test('has them once the last page of history carried any', () => {
    const page = history({kind: 'page', events: [SPAN], next: 2, complete: true});
    expect(traceArrival(fold([ENDED, NO_SUMMARY, page]))).toBe('arrived');
  });

  test('still awaits them after a page that is not the last: a prefix is not the run', () => {
    const page = history({kind: 'page', events: [SPAN], next: 2, complete: false});
    expect(traceArrival(fold([ENDED, NO_SUMMARY, page]))).toBe('awaited');
  });

  test('drops the prefix it folded when a later page fails, rather than show it as the run', () => {
    const first = history({kind: 'page', events: [SPAN], next: 2, complete: false});
    const failed = history({kind: 'failed', message: 'channel closed'});
    const state = fold([ENDED, NO_SUMMARY, first, failed]);
    expect(traceArrival(state)).toBe('none');
    expect(state.run.trace.events.length).toBe(0);
    expect(historyProblem(state)).toContain('channel closed');
    expect(historyLoading(state)).toBeNull();
  });

  test('awaits nothing once its history is read empty, refused or failed', () => {
    for (const reply of [
      {kind: 'page', events: [], next: 1, complete: true},
      {kind: 'unsupported'},
      {kind: 'failed', message: 'channel closed'},
    ] as const) {
      expect(traceArrival(fold([ENDED, NO_SUMMARY, history(reply)]))).toBe('none');
    }
  });
});

test('a run the host does not list awaits nothing', () => {
  expect(traceArrival(initial())).toBe('none');
});
