import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunSnapshot, Span} from '@vibesys/contracts';
import {type LogStream, parseHelperRequest} from '@vibesys/contracts/host-helper';
import type {HostRun} from '@vibesys/hosts';
import {emptyLogView, foldChunk} from '@vibesys/logs';
import type {RunTracePublication} from '@vibesys/run-session';
import {type CoreState, initialCoreState, reduceEvent} from '@vibesys/run-state';
import {checkModel, type Seeded} from '@vibesys/store/testing';
import {
  FIRST_HISTORY_SEQUENCE,
  HISTORY_PAGE_EVENTS,
  historyAwaiting,
  LOCATE_BOUND_MS,
  type RunPageAction,
  type RunPageRequest,
  type RunPageState,
  type RunRecord,
  runNow,
  runPageInitial,
  runPageInvariants,
  runPageStep,
  runWallNow,
  type Unavailable,
} from './run-page.js';
import type {SummaryReply} from './run-summary.js';
import {scrubberView} from './scrub.js';
import {
  endedView,
  followedStreams,
  headerView,
  logGroups,
  pauseNowBannerView,
  runGoneWords,
  streamLines,
  timelineView,
  viewedRun,
} from './views.js';

const RUN = {host: 'ssh:host-b', runId: '20261015-163047'} as const;

function server(phase: HostRun['phase'], extra: Partial<HostRun> = {}): HostRun {
  return {
    id: 'a1b2c3d4e5f6',
    projectRoot: '~/vibesys-projects/queue-rs',
    runId: RUN.runId,
    status: phase,
    phase,
    paused: phase === 'paused' ? 'user' : null,
    end: null,
    startedAt: 1,
    reason: null,
    ...extra,
  };
}

const RECORDS: readonly RunRecord[] = [
  {kind: 'unlisted', why: 'machine'},
  {kind: 'live', run: server('starting')},
  {kind: 'live', run: server('running')},
  {kind: 'live', run: server('pausing')},
  {kind: 'live', run: server('paused')},
  {kind: 'live', run: server('paused', {paused: 'quota'})},
  {kind: 'live', run: server('stopping')},
  {
    kind: 'ended',
    run: server('stopped', {
      end: {outcome: 'stopped', reason: 'Stopped by you.', resumable: false},
    }),
  },
  {
    kind: 'ended',
    run: server('failed', {
      end: {
        outcome: 'failed',
        reason: 'W6’s benchmark timed out after 300 s.',
        resumable: true,
        resume_from_round: 2,
      },
    }),
  },
  {kind: 'unlisted', why: 'unconfirmed'},
];

function stream(id: string, owner: LogStream['owner'], outcome: LogStream['outcome']): LogStream {
  return {id, owner, command: 'docker build .', retained_start: 0, size: 0, outcome};
}

const STREAMS: readonly LogStream[] = [
  stream('s1', {kind: 'startup_step', stage: 'building_image'}, 'ended'),
  stream('s2', {kind: 'startup_step', stage: 'starting_container'}, null),
  stream('s3', {kind: 'gate_stage', execution_id: 'w5', gate: 'benchmark', attempt: 1}, null),
  stream('s4', {kind: 'tool_build', name: 'perf'}, 'failed'),
  stream('s5', {kind: 'run_narration'}, null),
];

function initial(unavailable: Unavailable = {}): RunPageState {
  return runPageInitial({
    identity: RUN,
    title: {project: '~/vibesys-projects/queue-rs', task: 'mpmc'},
    hostLabel: 'host-b',
    unavailable,
  });
}

function fold(state: RunPageState, actions: readonly RunPageAction[]) {
  const requests: RunPageRequest[] = [];
  let current = state;
  for (const action of actions) {
    const step = runPageStep(current, action);
    current = step.state;
    requests.push(...step.requests);
  }
  return {state: current, requests};
}

function traceSpan(id: string): Span {
  return {id, parent: null, type: 'flow', name: id, status: 'done', started_at: 1};
}

function spanEvent(sequence: number, id: string): RunEvent {
  return {
    sequence,
    type: 'span_started',
    data: {kind: 'span_started', span: traceSpan(id)},
  } as RunEvent;
}

function attached(): RunPageState {
  const requested = fold(initial(), [{type: 'record', record: RECORDS[2] as RunRecord}]);
  const attach = requested.requests.find(request => request.kind === 'attach');
  if (attach?.kind !== 'attach') throw new Error('no attachment requested');
  return fold(requested.state, [{type: 'attached', token: attach.token, error: null}]).state;
}

function snapshot(sequence: number, ids: readonly string[]): RunSnapshot {
  return {run_id: RUN.runId, sequence, status: 'running', spans: ids.map(traceSpan)};
}

const USER_ACTIONS: readonly RunPageAction[] = [
  {type: 'ask-pause-now'},
  {type: 'cancel-pause-now'},
  {type: 'confirm-pause-now'},
  {type: 'pause-now-failed'},
  {type: 'pause'},
  {type: 'resume-ended'},
  {type: 'open-window'},
  {type: 'archive'},
  {type: 'run-again'},
  {type: 'view-logs'},
  {type: 'select-stream', streamId: 's3'},
  {type: 'agents-open', id: 'boss', open: true},
  {type: 'agents-open', id: 'boss', open: false},
  {type: 'ideas-layout', layout: 'tree'},
  {type: 'ideas-open', key: 'n1', open: true},
  {type: 'ideas-open', key: 'n1', open: false},
  {type: 'ideas-pin', idea: 'n2'},
  {type: 'ideas-pin', idea: null},
  {type: 'ideas-hover', idea: 'n3'},
  {type: 'ideas-hover', idea: null},
];

/** A span event at `sequence`, or another event the trace must skip. */
function runEvent(random: Seeded, sequence: number): RunEvent {
  const type = random.pick(['span_started', 'span_ended', 'agent_output_chunk'] as const);
  const span = {id: `s${random.int(4)}`, name: 'turn', status: 'running'};
  return {
    sequence,
    type,
    data: type === 'agent_output_chunk' ? null : {span},
  } as unknown as RunEvent;
}

function quotaEvent(sequence: number, data: Record<string, unknown>): RunEvent {
  return {sequence, type: data['kind'], data} as unknown as RunEvent;
}

const PAUSED = {
  kind: 'quota_paused',
  provider: 'claude',
  condition: 'quota_exhausted',
  detail: 'Claude usage limit reached',
  resets_at: 1_760_580_000,
};

/** The run fold after quota events: none, a pause, a pause with a wait, or a wait cancelled. */
function quotaCore(stage: number): CoreState {
  const events = [
    quotaEvent(1, PAUSED),
    quotaEvent(2, {kind: 'quota_waiting', provider: 'claude', resumes_at: 1_760_580_120}),
    quotaEvent(3, {kind: 'quota_wait_cancelled', provider: 'claude', reason: 'paused'}),
  ].slice(0, stage);
  return events.reduce(reduceEvent, initialCoreState());
}

/** What the attached run's session might report, current or late. */
function liveAction(random: Seeded, issued: readonly number[]): RunPageAction {
  switch (random.int(5)) {
    case 4:
      return {type: 'run-core', core: quotaCore(random.int(4))};
    case 0:
      return {
        type: 'attached',
        token: random.pick(issued),
        error: random.bool() ? null : 'refused',
      };
    case 1:
      return {
        type: 'session',
        session: {
          hasSnapshot: random.bool(),
          streaming: true,
          controls: random.bool(),
          error: null,
        },
      };
    case 2:
      if (random.bool()) {
        return {
          type: 'run-data',
          wallMs: 0,
          data: {
            kind: 'rebootstrap',
            base: random.pick(['preserve', 'reset'] as const),
            events: Array.from({length: random.int(4)}, () => runEvent(random, random.int(30))),
          },
        };
      }
      return {
        type: 'run-data',
        wallMs: 0,
        data: {
          kind: 'events',
          events: Array.from({length: random.int(4)}, () => runEvent(random, random.int(30))),
        },
      };
    case 3:
      return {type: 'view', view: random.bool() ? {at: 'live'} : {at: random.int(30)}};
    default:
      return {
        type: 'history',
        token: random.pick(issued),
        reply: random.pick([
          {kind: 'unsupported'} as const,
          {kind: 'failed', message: 'channel closed'} as const,
          {
            kind: 'page',
            events: Array.from({length: random.int(4)}, () => runEvent(random, random.int(30))),
            next: random.int(40),
            complete: random.bool(),
          } as const,
        ]),
      };
  }
}

function summaryOf(): SummaryReply {
  return {
    kind: 'summary',
    snapshot: {
      run_id: RUN.runId,
      sequence: 5,
      status: 'completed',
      spans: [traceSpan('flow:a')],
    },
    seed: [],
    through: 5,
    eventCount: 5,
    journalBytes: 100,
    trace: 'recorded',
    roundsFinished: 0,
    records: [],
    gates: [],
    terminal: null,
  };
}

/** A reply to a summary read (some for tokens the page never asked), of any of its outcomes. */
function summaryAction(random: Seeded, issued: readonly number[]): RunPageAction {
  const token = random.pick(issued);
  const reply = random.pick<SummaryReply>([
    {kind: 'unsupported'},
    {kind: 'failed', message: 'channel closed'},
    {kind: 'gone'},
    summaryOf(),
  ]);
  return {type: 'summary', token, reply, wallMs: 0};
}

function randomAction(random: Seeded, issued: readonly number[]): RunPageAction {
  switch (random.int(12)) {
    case 11:
      return {
        type: 'trace-page',
        token: random.pick(issued),
        reply: random.pick([
          {kind: 'unsupported'} as const,
          {kind: 'failed', message: 'channel closed'} as const,
          {
            kind: 'page',
            events: Array.from({length: random.int(4)}, () => runEvent(random, random.int(30))),
            next: random.int(40),
            complete: random.bool(),
          } as const,
        ]),
      };
    case 9:
      return summaryAction(random, issued);
    case 10:
      return {type: 'need-history'};
    case 6:
    case 7:
      return liveAction(random, issued);
    case 0:
      return {type: 'record', record: random.pick(RECORDS)};
    case 1:
      return {type: 'bound-elapsed', token: random.pick(issued)};
    case 2:
      return {
        type: 'streams',
        token: random.pick(issued),
        outcome: random.bool()
          ? {ok: true, instanceId: 'a1b2c3d4e5f6', streams: STREAMS.slice(0, random.int(6))}
          : {ok: false, failure: {code: 'transport', message: 'channel closed'}},
      };
    case 3:
      return {type: 'agents-open', id: random.pick(['p1', 'p2']), open: random.bool()};
    case 4:
      return {type: 'view', view: random.bool() ? {at: 'live'} : {at: random.int(40)}};
    default:
      return random.pick(USER_ACTIONS);
  }
}

function issues(request: RunPageRequest): number | null {
  switch (request.kind) {
    case 'arm-bound':
    case 'list-logs':
    case 'attach':
    case 'read-history':
    case 'read-trace':
    case 'read-summary':
      return request.token;
    default:
      return null;
  }
}

function awaited(state: RunPageState) {
  return {
    // Each armed bound (the stop's, the locate wait's) is its own slot, by its token.
    ...Object.fromEntries(
      [state.locate.kind === 'waiting' ? state.locate.bound : null].flatMap(token =>
        token === null ? [] : [[`bound:${token}`, token]],
      ),
    ),
    logs: state.logs.kind === 'listing' ? state.logs.token : null,
    attach: state.link.kind === 'attaching' ? state.link.token : null,
    // Each page in flight is its own slot: a reply is awaited by the token it was asked under.
    ...Object.fromEntries(historyAwaiting(state.history).map(token => [`history:${token}`, token])),
    summary: state.summary.kind === 'reading' ? state.summary.token : null,
    trace: state.traceHistory.kind === 'reading' ? state.traceHistory.token : null,
  };
}

function reply(action: RunPageAction) {
  if (action.type === 'bound-elapsed') return {slot: `bound:${action.token}`, token: action.token};
  if (action.type === 'streams') return {slot: 'logs', token: action.token};
  if (action.type === 'attached') return {slot: 'attach', token: action.token};
  if (action.type === 'history') return {slot: `history:${action.token}`, token: action.token};
  if (action.type === 'summary') return {slot: 'summary', token: action.token};
  if (action.type === 'trace-page') return {slot: 'trace', token: action.token};
  return null;
}

/** Why `request` was not on offer in `before`, or null when it was. */
function notOffered(before: RunPageState, request: RunPageRequest): string | null {
  switch (request.kind) {
    case 'pause':
      return headerView(before).pause.shown && headerView(before).pause.disabledReason === null
        ? null
        : 'pause was sent while not offered';
    case 'pause-now':
      return pauseNowBannerView(before).kind === 'confirm'
        ? null
        : 'cancellation was not confirmed';
    case 'resume-ended':
      return endedView(before)?.resume == null ? 'resume of a run that cannot resume' : null;
    case 'arm-bound':
      return request.ms === LOCATE_BOUND_MS ? null : 'a bound is not a documented one';
    default:
      return null;
  }
}

/** Each step's commands are ones the screen offered in the state before it. */
function offeredOnly(
  before: RunPageState,
  _action: RunPageAction,
  _after: RunPageState,
  requests: readonly RunPageRequest[],
): void {
  const problems = requests.map(request => notOffered(before, request)).filter(Boolean);
  if (problems.length > 0) throw new Error(problems.join('; '));
}

describe('the run page store', () => {
  test('holds its invariants over seeded action sequences, stale replies included', () => {
    for (const unavailable of [{}, {pause: 'no', logs: 'no', 'open-window': 'no'}]) {
      checkModel({
        initial: initial(unavailable),
        step: runPageStep,
        action: randomAction,
        issues,
        awaited,
        reply,
        invariants: runPageInvariants,
        also: offeredOnly,
      });
    }
  });

  test('quota holds stay internal while Pause now remains available', () => {
    for (const stage of [0, 1, 2, 3]) {
      const state = fold(attached(), [
        {type: 'record', record: {kind: 'live', run: server('paused', {paused: 'quota'})}},
        {type: 'run-core', core: quotaCore(stage)},
      ]).state;
      expect(Object.keys(headerView(state))).not.toContain('resume');
      expect(headerView(state).pauseNow.shown).toBe(true);
      expect(fold(state, [{type: 'pause'}]).requests).toEqual([]);
    }
  });

  test('same-store rebootstrap keeps the snapshot base and replaces the replay suffix', () => {
    const state = fold(attached(), [
      {type: 'run-data', wallMs: 0, data: {kind: 'snapshot', snapshot: snapshot(40, ['base'])}},
      {type: 'run-data', wallMs: 0, data: {kind: 'events', events: [spanEvent(41, 'old-tail')]}},
      {
        type: 'run-data',
        wallMs: 0,
        data: {
          kind: 'rebootstrap',
          base: 'preserve',
          events: [spanEvent(42, 'replayed-tail')],
        },
      },
    ]).state;

    expect(viewedRun(state).spans.map(span => span.id)).toEqual(['base', 'replayed-tail']);
    expect(state.run.trace.base.sequence).toBe(40);
    expect(state.run.trace.events.toArray().map(item => item.sequence)).toEqual([42]);
  });

  test('retains snapshot-only history when an event publication races ahead of the snapshot', () => {
    const publications: readonly RunTracePublication[] = [
      {kind: 'events', events: [spanEvent(2, 'live-tail')]},
      {kind: 'snapshot', snapshot: snapshot(1, ['historical-base'])},
    ];
    const state = fold(
      attached(),
      publications.map(data => ({type: 'run-data', wallMs: 0, data})),
    ).state;

    expect(viewedRun(state).spans.map(span => span.id)).toEqual(['historical-base', 'live-tail']);
    expect(state.run.trace.base.sequence).toBe(1);
    expect(state.run.trace.events.toArray().map(item => item.sequence)).toEqual([2]);
  });

  test('a proven replacement store resets both the trace base and replay suffix', () => {
    const state = fold(attached(), [
      {type: 'run-data', wallMs: 0, data: {kind: 'snapshot', snapshot: snapshot(40, ['old-base'])}},
      {type: 'run-data', wallMs: 0, data: {kind: 'events', events: [spanEvent(41, 'old-tail')]}},
      {
        type: 'run-data',
        wallMs: 0,
        data: {
          kind: 'rebootstrap',
          base: 'reset',
          events: [spanEvent(1, 'new-store')],
        },
      },
    ]).state;

    expect(viewedRun(state).spans.map(span => span.id)).toEqual(['new-store']);
    expect(state.run.trace.base.sequence).toBe(0);
    expect(state.run.trace.events.toArray().map(item => item.sequence)).toEqual([1]);
  });

  test('a fenced snapshot restores snapshot-only spans after an identity-unproven reset', () => {
    const publication: RunTracePublication = {
      kind: 'rebootstrap',
      base: 'reset',
      events: [spanEvent(2, 'identity-unknown')],
    };
    const reset = fold(attached(), [
      {type: 'run-data', wallMs: 0, data: {kind: 'snapshot', snapshot: snapshot(40, ['old-base'])}},
      {type: 'run-data', wallMs: 0, data: {kind: 'events', events: [spanEvent(41, 'old-tail')]}},
      {type: 'run-data', wallMs: 0, data: publication},
    ]).state;
    expect(viewedRun(reset).spans.map(span => span.id)).toEqual(['identity-unknown']);

    const restored = fold(reset, [
      {
        type: 'run-data',
        wallMs: 0,
        data: {kind: 'snapshot', snapshot: snapshot(2, ['old-base', 'identity-unknown'])},
      },
    ]).state;
    expect(viewedRun(restored).spans.map(span => span.id)).toEqual([
      'old-base',
      'identity-unknown',
    ]);
    expect(restored.run.trace.base.sequence).toBe(2);
    expect(restored.run.trace.events.toArray()).toEqual([]);
  });

  test('Pause now is offered during a drain with the cancellation warning', () => {
    const draining = fold(initial(), [{type: 'record', record: RECORDS[6] as RunRecord}]).state;
    expect(headerView(draining).pauseNow.shown).toBe(true);
    const asked = fold(draining, [{type: 'ask-pause-now'}]);
    expect(pauseNowBannerView(asked.state)).toEqual({
      kind: 'confirm',
      title: 'Pause now?',
      body: 'Unfinished work may need repeating and may still incur charges.',
    });
    expect(fold(asked.state, [{type: 'cancel-pause-now'}]).state.pauseNow.kind).toBe('idle');
  });

  test('the scrubber is page state that asks for no I/O', () => {
    const {state, requests} = fold(initial(), [{type: 'view', view: {at: 12}}]);
    expect(requests).toEqual([]);
    expect(state.view).toEqual({at: 12});
    expect(fold(state, [{type: 'view', view: {at: 'live'}}]).state.view).toEqual({at: 'live'});
  });

  test('the ended banner names the round to resume from only when resumable', () => {
    const stopped = fold(initial(), [{type: 'record', record: RECORDS[7] as RunRecord}]).state;
    expect(endedView(stopped)).toMatchObject({title: 'Stopped', resume: 'Resume'});
    const failed = fold(initial(), [{type: 'record', record: RECORDS[8] as RunRecord}]).state;
    expect(endedView(failed)).toMatchObject({title: 'Failed', resume: 'Resume'});
    expect(fold(failed, [{type: 'resume-ended'}]).requests).toEqual([
      {kind: 'resume-ended', fromRound: 2},
    ]);
  });

  test('regression: a run that ended with no winner is not shown as a success', () => {
    const record: RunRecord = {
      kind: 'ended',
      run: server('completed', {
        end: {outcome: 'no-winner', reason: 'No candidate beat the input.', resumable: false},
      }),
    };
    const state = fold(initial(), [{type: 'record', record}]).state;
    expect(headerView(state).status).toEqual({label: 'No winner', tone: 'neutral'});
    expect(endedView(state)).toMatchObject({title: 'No winner', tone: 'neutral'});
  });

  test('groups streams by owner and builds the startup timeline from them', () => {
    const listed = fold(initial(), [{type: 'record', record: RECORDS[1] as RunRecord}]);
    const list = listed.requests.find(request => request.kind === 'list-logs');
    if (list?.kind !== 'list-logs') throw new Error('no listing asked');
    const {state} = fold(listed.state, [
      {
        type: 'streams',
        token: list.token,
        outcome: {ok: true, instanceId: 'a1b2c3d4e5f6', streams: STREAMS},
      },
    ]);
    expect(logGroups(state).map(group => group.name)).toEqual([
      'Startup',
      'Gates',
      'Tool builds',
      'Run narration',
    ]);
    expect(timelineView(state).map(stage => stage.state)).toEqual(['done', 'done', 'active']);
    const again = fold(state, [{type: 'record', record: RECORDS[2] as RunRecord}]).requests.find(
      request => request.kind === 'list-logs',
    );
    if (again?.kind !== 'list-logs') throw new Error('no relisting asked');
    const restarted = fold(state, [
      {type: 'record', record: RECORDS[2] as RunRecord},
      {
        type: 'streams',
        token: again.token,
        outcome: {
          ok: true,
          instanceId: 'a1b2c3d4e5f6',
          streams: [
            ...STREAMS,
            stream('s6', {kind: 'startup_step', stage: 'building_image'}, null),
          ],
        },
      },
    ]).state;
    expect(logGroups(restarted)[0]?.streams.map(item => item.label)).toEqual([
      'Building the agent image · 1',
      'Starting the agent container',
      'Building the agent image · 2',
    ]);
    expect(followedStreams(state)).toEqual(['s1', 's2']);
    const narration = foldChunk(emptyLogView(), {
      offset: 0,
      data: '{"at":"2026-10-15T16:30:51+00:00","level":"INFO","logger":"vibesys","thread":"main","message":"run started"}\n',
      next_offset: 120,
      eof: true,
      closed: false,
      truncated_before: false,
      pipes: [],
    });
    const shown = fold(state, [
      {type: 'log-view', streamId: 's5', view: narration},
      {type: 'view-logs'},
    ]).state;
    expect(shown.followingLogs).toBe(true);
    expect(streamLines(shown, 's5')).toEqual([{text: '16:30:51 INFO run started', stream: 'out'}]);
  });

  test("a measurement's candidate build is its own stream under Gates, before its check", () => {
    const listed = fold(initial(), [{type: 'record', record: RECORDS[1] as RunRecord}]);
    const list = listed.requests.find(request => request.kind === 'list-logs');
    if (list?.kind !== 'list-logs') throw new Error('no listing asked');
    const gate = (id: string, name: 'build' | 'accuracy' | 'benchmark'): LogStream =>
      stream(id, {kind: 'gate_stage', execution_id: 'w5', gate: name, attempt: 1}, 'ended');
    const {state} = fold(listed.state, [
      {
        type: 'streams',
        token: list.token,
        outcome: {
          ok: true,
          instanceId: 'a1b2c3d4e5f6',
          streams: [gate('b1', 'build'), gate('b2', 'accuracy'), gate('b3', 'benchmark')],
        },
      },
    ]);

    const gates = logGroups(state).find(group => group.name === 'Gates');

    expect(gates?.streams.map(item => item.label)).toEqual([
      'candidate build · input · attempt 1',
      'accuracy · input · attempt 1',
      'benchmark · input · attempt 1',
    ]);
  });

  test('an instance replacement fences its old log listing and projected bytes', () => {
    const first = fold(initial(), [{type: 'record', record: RECORDS[1] as RunRecord}]);
    const firstList = first.requests.find(request => request.kind === 'list-logs');
    if (firstList?.kind !== 'list-logs') throw new Error('no first listing asked');
    expect(firstList.instanceId).toBe('a1b2c3d4e5f6');

    const oldView = foldChunk(emptyLogView(), {
      offset: 0,
      data: 'bytes from the replaced instance\n',
      next_offset: 33,
      eof: false,
      closed: false,
      truncated_before: false,
      pipes: [],
    });
    const withOldBytes = fold(first.state, [
      {type: 'log-view', streamId: 's1', view: oldView},
    ]).state;
    const replacement = {
      kind: 'live',
      run: server('starting', {id: 'fedcba654321'}),
    } as const;
    const replaced = fold(withOldBytes, [{type: 'record', record: replacement}]);
    const secondList = replaced.requests.find(request => request.kind === 'list-logs');
    if (secondList?.kind !== 'list-logs') throw new Error('no replacement listing asked');

    expect(secondList.instanceId).toBe('fedcba654321');
    expect(secondList.token).not.toBe(firstList.token);
    expect(replaced.state.views).toEqual({});
    expect(replaced.state.logs).toMatchObject({
      kind: 'listing',
      token: secondList.token,
      instanceId: 'fedcba654321',
    });

    const stale = runPageStep(replaced.state, {
      type: 'streams',
      token: firstList.token,
      outcome: {ok: true, instanceId: 'a1b2c3d4e5f6', streams: STREAMS},
    });
    expect(stale.state).toBe(replaced.state);

    const mismatched = runPageStep(replaced.state, {
      type: 'streams',
      token: secondList.token,
      outcome: {ok: true, instanceId: 'a1b2c3d4e5f6', streams: STREAMS},
    });
    expect(mismatched.state.logs).toEqual({
      kind: 'failed',
      message: 'the app listed logs for a1b2c3d4e5f6, not fedcba654321',
      unsupported: false,
    });
    expect(mismatched.state.views).toEqual({});
  });

  test('a run watched to its end keeps what its session folded and reads no history', () => {
    const live = fold(initial(), [{type: 'record', record: RECORDS[2] as RunRecord}]);
    const attach = live.requests.find(request => request.kind === 'attach');
    if (attach?.kind !== 'attach') throw new Error('no attach asked');
    const span = {
      sequence: 3,
      type: 'span_started',
      timestamp: '2026-10-15T16:30:51Z',
      data: {span: {id: 'flow:w1'}},
    } as unknown as RunEvent;
    const watched = fold(live.state, [
      {type: 'attached', token: attach.token, error: null},
      {type: 'run-data', wallMs: 0, data: {kind: 'events', events: [span]}},
    ]).state;
    const ended = fold(watched, [{type: 'record', record: RECORDS[8] as RunRecord}]);
    expect(ended.requests.some(request => request.kind === 'read-history')).toBe(false);
    expect(ended.state.run).toBe(watched.run);
    expect(ended.state.history).toEqual({kind: 'read'});
  });

  // Regression: a run opened as it started asks for its summary before its host has a journal,
  // is told it holds no record, and kept that answer until the run ended: the finished run showed
  // "this run is gone" with the live view replaced, until the user opened another run and back.
  const LIVE_RECORDS = RECORDS.filter(record => record.kind === 'live');
  const ENDED_RECORDS = RECORDS.filter(record => record.kind === 'ended');
  for (const live of LIVE_RECORDS) {
    for (const ended of ENDED_RECORDS) {
      test(`a ${live.run.phase} run the host had no journal of is read again once it is ${ended.run.phase}`, () => {
        const opened = fold(initial(), [{type: 'record', record: live}]);
        const asked = opened.requests.find(request => request.kind === 'read-summary');
        if (asked?.kind !== 'read-summary') throw new Error('no summary asked');
        const early = fold(opened.state, [
          {type: 'summary', token: asked.token, reply: {kind: 'gone'}, wallMs: 0},
        ]);
        expect(early.state.summary).toEqual({kind: 'idle'});

        const over = fold(early.state, [{type: 'record', record: ended}]);
        expect(runGoneWords(over.state)).toBeNull();
        const again = over.requests.find(request => request.kind === 'read-summary');
        if (again?.kind !== 'read-summary') throw new Error('the finished run was not read again');
        const read = fold(over.state, [
          {type: 'summary', token: again.token, reply: summaryOf(), wallMs: 0},
        ]);
        expect(read.state.summary.kind).toBe('ready');
        expect(runGoneWords(read.state)).toBeNull();
      });
    }
  }

  test('a run opened ended that its host has no record of stays gone', () => {
    const opened = fold(initial(), [{type: 'record', record: RECORDS[7] as RunRecord}]);
    const asked = opened.requests.find(request => request.kind === 'read-summary');
    if (asked?.kind !== 'read-summary') throw new Error('no summary asked');
    const gone = fold(opened.state, [
      {type: 'summary', token: asked.token, reply: {kind: 'gone'}, wallMs: 0},
    ]);
    expect(runGoneWords(gone.state)).not.toBeNull();
    expect(gone.requests).toEqual([]);
  });

  test('a run its session folded is not gone when its late summary read says the host has no record', () => {
    const opened = fold(initial(), [{type: 'record', record: RECORDS[2] as RunRecord}]);
    const asked = opened.requests.find(request => request.kind === 'read-summary');
    if (asked?.kind !== 'read-summary') throw new Error('no summary asked');
    const watched = fold(opened.state, [
      {type: 'run-data', data: {kind: 'snapshot', snapshot: snapshot(3, ['a'])}, wallMs: 0},
      {type: 'summary', token: asked.token, reply: {kind: 'gone'}, wallMs: 0},
      {type: 'record', record: RECORDS[7] as RunRecord},
    ]);
    expect(runGoneWords(watched.state)).toBeNull();
  });

  test('an ended run an operator resumes is no longer gone', () => {
    const opened = fold(initial(), [{type: 'record', record: RECORDS[7] as RunRecord}]);
    const asked = opened.requests.find(request => request.kind === 'read-summary');
    if (asked?.kind !== 'read-summary') throw new Error('no summary asked');
    const resumed = fold(opened.state, [
      {type: 'summary', token: asked.token, reply: {kind: 'gone'}, wallMs: 0},
      {type: 'record', record: RECORDS[2] as RunRecord},
    ]);
    expect(resumed.state.summary.kind).not.toBe('gone');
  });

  test('an ended run reads its history page by page into the run, and the tabs see its spans', () => {
    const span = (sequence: number, id: string, type: string): RunEvent =>
      ({
        sequence,
        type,
        timestamp: '2026-10-15T16:30:51Z',
        data: {span: {id}},
      }) as unknown as RunEvent;
    const opened = fold(initial(), [{type: 'record', record: RECORDS[8] as RunRecord}]);
    const asked = opened.requests.find(request => request.kind === 'read-summary');
    if (asked?.kind !== 'read-summary') throw new Error('no summary asked');
    // A host that serves no summary (an older helper) is read through its history.
    const ended = fold(opened.state, [
      {type: 'summary', token: asked.token, reply: {kind: 'unsupported'}, wallMs: 0},
    ]);
    const first = ended.requests.find(request => request.kind === 'read-history');
    expect(first).toMatchObject({from: FIRST_HISTORY_SEQUENCE, max: HISTORY_PAGE_EVENTS});
    if (first?.kind !== 'read-history') throw new Error('no history asked');
    // Regression: the first read asked from 0, which the helper refuses (`from_sequence` is
    // positive), so every run opened ended showed nothing.
    expect(() => parseHelperRequest(readEventsLine(first.from, first.max))).not.toThrow();
    const paged = fold(ended.state, [
      {
        type: 'history',
        token: first.token,
        reply: {kind: 'page', events: [span(1, 'a', 'span_started')], next: 2, complete: false},
      },
    ]);
    const second = paged.requests.find(request => request.kind === 'read-history');
    expect(second).toMatchObject({from: 2});
    if (second?.kind !== 'read-history') throw new Error('no second page asked');
    const read = fold(paged.state, [
      {
        type: 'history',
        token: second.token,
        reply: {kind: 'page', events: [span(2, 'b', 'span_started')], next: 3, complete: true},
      },
    ]).state;
    expect(read.history).toEqual({kind: 'read'});
    expect(viewedRun(read).spans.map(each => each.id)).toEqual(['a', 'b']);
    expect(viewedRun({...read, view: {at: 1}}).spans.map(each => each.id)).toEqual(['a']);
    expect(read.run.core).not.toBeNull();
  });
});

/** The `runs.read_events` line a history read of `from` and `max` sends to the host's helper. */
function readEventsLine(from: number, max: number): string {
  return JSON.stringify({
    type: 'runs.read_events',
    request_id: 'history-1',
    deadline_ms: 30_000,
    project: '/projects/queue',
    run_id: 'run-1',
    from_sequence: from,
    max,
  });
}

/** An event at `clock` (UTC time of day on 2026-10-17); a span event carries `span`. */
function liveEvent(sequence: number, clock: string, type: string, span?: Span): RunEvent {
  return {
    protocol_version: 1,
    run_id: RUN.runId,
    sequence,
    timestamp: `2026-10-17T${clock}Z`,
    type,
    data: span === undefined ? {kind: type} : {kind: type, span},
  } as RunEvent;
}

/** 2026-10-17T01:23:31Z on the run's own clock, the epoch seconds its spans carry. */
const AT_012331 = 1792200211;

function round(status: Span['status']): Span {
  return {
    id: 'flow:round/#1',
    parent: null,
    type: 'flow',
    name: 'play_round',
    status,
    started_at: AT_012331,
  };
}

describe('the Agents list folds', () => {
  test('the page keeps each unfolded group through every other action, and asks for nothing', () => {
    let state = runPageStep(attached(), {type: 'agents-open', id: 'boss', open: true}).state;
    const open = state.agentsOpen;
    expect([...open]).toEqual(['boss']);
    const others = USER_ACTIONS.filter(each => each.type !== 'agents-open');
    for (const action of [...others, ...[...others].reverse()]) {
      state = runPageStep(state, action).state;
      expect(state.agentsOpen).toBe(open);
    }
    const again = runPageStep(state, {type: 'agents-open', id: 'boss', open: true});
    expect(again.requests).toEqual([]);
    expect(again.state.agentsOpen).toBe(open);
    const lead = runPageStep(state, {type: 'agents-open', id: 'lead', open: true}).state;
    expect([...lead.agentsOpen].sort()).toEqual(['boss', 'lead']);
  });
});

describe('the live run clock', () => {
  // The host-b run: the last span changes at 01:23:36, then a pause, a resume and a control
  // change no span time until 01:27:43; the window hears them and ticks 17 s after the last.
  const waiting = {kind: 'operation', ref: 'resume of evaluation', since: AT_012331 + 5};
  const events = [
    liveEvent(1, '01:23:31', 'span_started', round('running')),
    liveEvent(2, '01:23:36', 'span_updated', {...round('running'), waiting_on: [waiting]} as Span),
    liveEvent(3, '01:24:19', 'run_status_changed'),
    liveEvent(4, '01:24:20', 'run_status_changed'),
    liveEvent(5, '01:25:32', 'run_status_changed'),
    liveEvent(6, '01:27:43', 'control'),
  ];
  const NOW = AT_012331 + (4 * 60 + 12) + 17;

  function heard(batches: readonly (readonly RunEvent[])[]): RunPageState {
    const last = batches.length - 1;
    return fold(attached(), [
      ...batches.map((batch, index) => ({
        type: 'run-data' as const,
        wallMs: 1000 * index,
        data: {kind: 'events' as const, events: batch},
      })),
      {type: 'tick', wallMs: 1000 * last + 17_000},
    ]).state;
  }

  test('follows the run’s newest event, even one that changes no span time', () => {
    // However the events arrive in batches, the clock reads the newest one's time.
    for (let cut = 1; cut <= events.length; cut += 1) {
      const batches = [events.slice(0, cut), events.slice(cut)].filter(each => each.length > 0);
      const state = heard(batches);
      const now = runNow(state);
      expect(now).toBe(NOW);
      const shown = scrubberView(state.run.trace, state.view, false, runWallNow(state));
      expect(shown?.position).toStartWith('01:28:00');
    }
  });

  test('reads on a page opened on a snapshot before any span event follows it', () => {
    // A window reopened on a live run gets a snapshot, then only events that change no span. The
    // row still shows: one position, at the snapshot's time, carried on the window clock.
    const opened: RunSnapshot = {
      run_id: RUN.runId,
      sequence: 40,
      status: 'running',
      spans: [round('running')],
    };
    for (const later of [[], [liveEvent(41, '01:24:00', 'run_status_changed')]]) {
      const state = fold(attached(), [
        {type: 'run-data', wallMs: 0, data: {kind: 'snapshot', snapshot: opened}},
        ...(later.length === 0
          ? []
          : [
              {
                type: 'run-data' as const,
                wallMs: 0,
                data: {kind: 'events' as const, events: later},
              },
            ]),
        {type: 'tick', wallMs: 17_000},
      ]).state;
      const now = runNow(state);
      expect(now).toBe(AT_012331 + 17);
      const shown = scrubberView(state.run.trace, state.view, false, runWallNow(state));
      expect(shown).toMatchObject({last: 0, value: 0, latest: true});
      expect(shown?.position).toStartWith('01:23:48');
      // Once the run has ended, the same position reads the snapshot's own time.
      expect(scrubberView(state.run.trace, state.view, true, 0)?.position).toStartWith('01:23:31');
    }
  });

  test('moves only forward, and a past view reads no live clock', () => {
    // A late event stamped earlier moves nothing: the clock carries on from 01:27:43, 18 s later.
    const state = heard([events, [liveEvent(7, '01:20:00', 'control')]]);
    expect(runNow(state)).toBe(NOW + 1);
    expect(runNow({...state, view: {at: 2}})).toBeNull();
  });
});

describe('the header’s token figure', () => {
  const live = (): RunPageState =>
    runPageStep(
      runPageInitial({
        identity: RUN,
        title: {project: '~/p', task: 'mpmc'},
        hostLabel: 'host-b',
        unavailable: {},
      }),
      {type: 'record', record: {kind: 'live', run: server('running')}},
    ).state;
  const usage = (sequence: number, tokens: Record<string, number>): RunEvent =>
    ({
      protocol_version: 1,
      run_id: RUN.runId,
      sequence,
      timestamp: '2026-01-01T00:00:00Z',
      type: 'turn_usage_recorded',
      data: {kind: 'turn_usage_recorded', ...tokens},
    }) as RunEvent;
  const withCore = (state: RunPageState, events: readonly RunEvent[]): RunPageState =>
    runPageStep(
      {...state, link: {kind: 'attached', instance: 'a1b2c3d4e5f6'}},
      {type: 'run-core', core: events.reduce(reduceEvent, initialCoreState())},
    ).state;

  test('is absent until a turn reports tokens', () => {
    expect(headerView(live()).spent).toBeNull();
    expect(headerView(withCore(live(), [usage(1, {})])).spent).toBeNull();
  });

  test('sums every turn, with the exact counts for the hover, and follows each new turn', () => {
    const first = withCore(live(), [usage(1, {input_tokens: 1_000_000, output_tokens: 20_000})]);
    expect(headerView(first).spent?.label).toBe('in 1M (cached unknown) · out 20k');
    const second = withCore(live(), [
      usage(1, {input_tokens: 1_000_000, output_tokens: 20_000}),
      usage(2, {input_tokens: 150_000, cache_read_input_tokens: 100_000, output_tokens: 30_000}),
    ]);
    const spent = headerView(second).spent;
    expect(spent?.label).toBe('in 1.2M (100k cached) · out 50k');
    expect(spent?.title).toBe(
      'Input 1,150,000 (100,000 cached, 1,050,000 uncached)\nOutput 50,000\nTotal 1,200,000',
    );
  });
});
