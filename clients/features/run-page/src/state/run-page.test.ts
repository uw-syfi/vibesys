import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunSnapshot, Span} from '@vibesys/contracts';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun} from '@vibesys/hosts';
import {emptyLogView, foldChunk} from '@vibesys/logs';
import type {RunTracePublication} from '@vibesys/run-session';
import {type CoreState, initialCoreState, reduceEvent} from '@vibesys/run-state';
import {checkModel, type Seeded} from '@vibesys/store/testing';
import {
  endedView,
  FORCE_STOP_BOUND_MS,
  followedStreams,
  HISTORY_PAGE_EVENTS,
  headerView,
  logGroups,
  MAP_FILTERS,
  quotaBannerView,
  RUN_TABS,
  type RunPageAction,
  type RunPageRequest,
  type RunPageState,
  type RunRecord,
  runPageInitial,
  runPageInvariants,
  runPageStep,
  runPhase,
  stopBannerView,
  streamLines,
  timelineView,
  type Unavailable,
  viewedRun,
} from './index.js';

const RUN = {host: 'ssh:chelan4', runId: '20261015-163047'} as const;

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
  {kind: 'unlisted'},
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
    hostLabel: 'chelan4',
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
  {type: 'ask-stop'},
  {type: 'cancel-stop'},
  {type: 'confirm-stop'},
  {type: 'force-stop'},
  {type: 'stop-failed'},
  {type: 'pause'},
  {type: 'resume'},
  {type: 'wait-for-quota'},
  {type: 'stop-waiting'},
  {type: 'wait-refused', message: "unknown field 'when'"},
  {type: 'resume-ended'},
  {type: 'open-window'},
  {type: 'archive'},
  {type: 'run-again'},
  {type: 'view-logs'},
  {type: 'select-stream', streamId: 's3'},
  {type: 'work-query', query: 'ring'},
  {type: 'work-outcome', outcome: 'accepted'},
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
          data: {
            kind: 'rebootstrap',
            base: random.pick(['preserve', 'reset'] as const),
            events: Array.from({length: random.int(4)}, () => runEvent(random, random.int(30))),
          },
        };
      }
      return {
        type: 'run-data',
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

function randomAction(random: Seeded, issued: readonly number[]): RunPageAction {
  switch (random.int(9)) {
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
      return {type: 'tab', tab: random.pick(RUN_TABS)};
    case 4:
      return random.pick<RunPageAction>([
        {type: 'map-filter', filter: random.pick(MAP_FILTERS)},
        {type: 'map-toggle', id: random.pick(['p1', 'p2']), open: random.bool()},
        {type: 'map-select', id: random.pick(['root', 'w1'])},
        {type: 'view', view: random.bool() ? {at: 'live'} : {at: random.int(40)}},
      ]);
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
      return request.token;
    default:
      return null;
  }
}

function awaited(state: RunPageState) {
  return {
    bound: state.stop.kind === 'stopping' ? state.stop.bound : null,
    logs: state.logs.kind === 'listing' ? state.logs.token : null,
    attach: state.link.kind === 'attaching' ? state.link.token : null,
    history: state.history.kind === 'reading' ? state.history.token : null,
  };
}

function reply(action: RunPageAction) {
  if (action.type === 'bound-elapsed') return {slot: 'bound', token: action.token};
  if (action.type === 'streams') return {slot: 'logs', token: action.token};
  if (action.type === 'attached') return {slot: 'attach', token: action.token};
  if (action.type === 'history') return {slot: 'history', token: action.token};
  return null;
}

/** Why a Pause or Resume was not on offer in `before` (header or quota banner), or null. */
function commandNotOffered(
  before: RunPageState,
  request: Extract<RunPageRequest, {kind: 'pause' | 'resume'}>,
): string | null {
  const header = headerView(before);
  const banner = quotaBannerView(before, String);
  const offers =
    request.kind === 'pause'
      ? [header.pause, banner?.stopWaiting]
      : request.when === 'now'
        ? [header.resume, banner?.resumeNow]
        : [banner?.wait];
  const offered = offers.some(each => each?.shown === true && each.disabledReason === null);
  return offered ? null : `${request.kind} was sent while not offered`;
}

/** Why `request` was not on offer in `before`, or null when it was. */
function notOffered(before: RunPageState, request: RunPageRequest): string | null {
  switch (request.kind) {
    case 'pause':
    case 'resume':
      return commandNotOffered(before, request);
    case 'stop': {
      const banner = stopBannerView(before);
      const offered = !request.force || (banner.kind === 'stopping' && banner.forceOffered);
      return offered ? null : 'force stop was sent while not offered';
    }
    case 'resume-ended':
      return endedView(before)?.resume == null ? 'resume of a run that cannot resume' : null;
    case 'arm-bound':
      return request.ms === FORCE_STOP_BOUND_MS
        ? null
        : 'the force bound is not the documented one';
    default:
      return null;
  }
}

/** Each step's commands are ones the screen offered in the state before it. */
function offeredOnly(
  before: RunPageState,
  _action: RunPageAction,
  after: RunPageState,
  requests: readonly RunPageRequest[],
): void {
  const problems = requests.map(request => notOffered(before, request)).filter(Boolean);
  if (problems.length > 0) throw new Error(problems.join('; '));
  if (stopBannerView(after).kind === 'stopping' && runPhase(after).kind !== 'stopping')
    throw new Error('a stopping banner outside stopping');
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

  test('a quota pause offers Resume now and the wait, and the wait offers Stop waiting', () => {
    const at = (seconds: number) => `t${seconds}`;
    const attached = fold(initial(), [
      {type: 'record', record: {kind: 'live', run: server('paused', {paused: 'quota'})}},
    ]);
    const token = attached.requests.find(request => request.kind === 'attach');
    const ready = fold(attached.state, [
      {type: 'attached', token: token?.kind === 'attach' ? token.token : 0, error: null},
      {type: 'session', session: {hasSnapshot: true, streaming: true, controls: true, error: null}},
      {type: 'run-core', core: quotaCore(1)},
    ]).state;
    expect(headerView(ready).resume.shown).toBe(false);
    expect(quotaBannerView(ready, at)).toMatchObject({
      title: 'Paused: Claude hit its usage limit',
      body: 'Claude usage limit reached. Claude expects capacity back at t1760580000.',
      wait: {shown: true},
      stopWaiting: {shown: false},
    });
    expect(fold(ready, [{type: 'wait-for-quota'}]).requests).toEqual([
      {kind: 'resume', when: 'capacity_returns'},
    ]);
    expect(fold(ready, [{type: 'resume'}]).requests).toEqual([{kind: 'resume', when: 'now'}]);
    // Stop waiting is not on offer before the run waits.
    expect(fold(ready, [{type: 'stop-waiting'}]).requests).toEqual([]);

    const waiting = fold(ready, [{type: 'run-core', core: quotaCore(2)}]).state;
    expect(quotaBannerView(waiting, at)).toMatchObject({
      title: "Waiting for Claude's quota",
      body: 'The run resumes by itself at about t1760580120.',
      wait: {shown: false},
      stopWaiting: {shown: true},
    });
    expect(fold(waiting, [{type: 'stop-waiting'}]).requests).toEqual([{kind: 'pause'}]);
    expect(fold(waiting, [{type: 'wait-for-quota'}]).requests).toEqual([]);

    const cancelled = fold(waiting, [{type: 'run-core', core: quotaCore(3)}]).state;
    expect(quotaBannerView(cancelled, at)?.wait.shown).toBe(true);
    // A run paused by its user shows no quota banner.
    const byUser = fold(cancelled, [
      {type: 'record', record: {kind: 'live', run: server('paused')}},
    ]).state;
    expect(quotaBannerView(byUser, at)).toBeNull();
  });

  test('same-store rebootstrap keeps the snapshot base and replaces the replay suffix', () => {
    const state = fold(attached(), [
      {type: 'run-data', data: {kind: 'snapshot', snapshot: snapshot(40, ['base'])}},
      {type: 'run-data', data: {kind: 'events', events: [spanEvent(41, 'old-tail')]}},
      {
        type: 'run-data',
        data: {
          kind: 'rebootstrap',
          base: 'preserve',
          events: [spanEvent(42, 'replayed-tail')],
        },
      },
    ]).state;

    expect(viewedRun(state).spans.map(span => span.id)).toEqual(['base', 'replayed-tail']);
    expect(state.run.trace.base.sequence).toBe(40);
    expect(state.run.trace.events.map(item => item.sequence)).toEqual([42]);
  });

  test('retains snapshot-only history when an event publication races ahead of the snapshot', () => {
    const publications: readonly RunTracePublication[] = [
      {kind: 'events', events: [spanEvent(2, 'live-tail')]},
      {kind: 'snapshot', snapshot: snapshot(1, ['historical-base'])},
    ];
    const state = fold(
      attached(),
      publications.map(data => ({type: 'run-data', data})),
    ).state;

    expect(viewedRun(state).spans.map(span => span.id)).toEqual(['historical-base', 'live-tail']);
    expect(state.run.trace.base.sequence).toBe(1);
    expect(state.run.trace.events.map(item => item.sequence)).toEqual([2]);
  });

  test('a proven replacement store resets both the trace base and replay suffix', () => {
    const state = fold(attached(), [
      {type: 'run-data', data: {kind: 'snapshot', snapshot: snapshot(40, ['old-base'])}},
      {type: 'run-data', data: {kind: 'events', events: [spanEvent(41, 'old-tail')]}},
      {
        type: 'run-data',
        data: {
          kind: 'rebootstrap',
          base: 'reset',
          events: [spanEvent(1, 'new-store')],
        },
      },
    ]).state;

    expect(viewedRun(state).spans.map(span => span.id)).toEqual(['new-store']);
    expect(state.run.trace.base.sequence).toBe(0);
    expect(state.run.trace.events.map(item => item.sequence)).toEqual([1]);
  });

  test('a fenced snapshot restores snapshot-only spans after an identity-unproven reset', () => {
    const publication: RunTracePublication = {
      kind: 'rebootstrap',
      base: 'reset',
      events: [spanEvent(2, 'identity-unknown')],
    };
    const reset = fold(attached(), [
      {type: 'run-data', data: {kind: 'snapshot', snapshot: snapshot(40, ['old-base'])}},
      {type: 'run-data', data: {kind: 'events', events: [spanEvent(41, 'old-tail')]}},
      {type: 'run-data', data: publication},
    ]).state;
    expect(viewedRun(reset).spans.map(span => span.id)).toEqual(['identity-unknown']);

    const restored = fold(reset, [
      {
        type: 'run-data',
        data: {kind: 'snapshot', snapshot: snapshot(2, ['old-base', 'identity-unknown'])},
      },
    ]).state;
    expect(viewedRun(restored).spans.map(span => span.id)).toEqual([
      'old-base',
      'identity-unknown',
    ]);
    expect(restored.run.trace.base.sequence).toBe(2);
    expect(restored.run.trace.events).toEqual([]);
  });

  test('offers force stop only after the bound elapses while stopping', () => {
    const live = {type: 'record', record: RECORDS[2]} as RunPageAction;
    const asked = fold(initial(), [live, {type: 'ask-stop'}, {type: 'confirm-stop'}]);
    expect(asked.requests).toContainEqual({kind: 'stop', force: false});
    const arm = asked.requests.find(request => request.kind === 'arm-bound');
    if (arm?.kind !== 'arm-bound') throw new Error('no bound armed');
    expect(stopBannerView(asked.state)).toMatchObject({kind: 'stopping', forceOffered: false});
    // A stale bound changes nothing.
    expect(runPageStep(asked.state, {type: 'bound-elapsed', token: arm.token + 99}).state).toBe(
      asked.state,
    );
    const elapsed = fold(asked.state, [{type: 'bound-elapsed', token: arm.token}]);
    expect(stopBannerView(elapsed.state)).toMatchObject({kind: 'stopping', forceOffered: true});
    const forced = fold(elapsed.state, [{type: 'force-stop'}]);
    expect(forced.requests).toEqual([{kind: 'stop', force: true}]);
    // The run ends: no stop flow survives, and a late bound is dropped.
    const ended = fold(forced.state, [
      {type: 'record', record: RECORDS[7] as RunRecord},
      {type: 'bound-elapsed', token: arm.token},
    ]);
    expect(ended.state.stop).toEqual({kind: 'idle'});
  });

  test('the Map and the scrubber are page state that asks for no I/O', () => {
    const {state, requests} = fold(initial(), [
      {type: 'map-filter', filter: 'failed'},
      {type: 'map-toggle', id: 'p1', open: true},
      {type: 'map-toggle', id: 'p2', open: true},
      {type: 'map-toggle', id: 'p1', open: false},
      {type: 'map-select', id: 'w1'},
      {type: 'view', view: {at: 12}},
    ]);
    expect(requests).toEqual([]);
    expect(state.map).toEqual({filter: 'failed', open: new Set(['p2']), selected: 'w1'});
    expect(state.view).toEqual({at: 12});
    expect(fold(state, [{type: 'view', view: {at: 'live'}}]).state.view).toEqual({at: 'live'});
  });

  test('a stop seen in the record (from anywhere) arms the same bound', () => {
    const {state, requests} = fold(initial(), [{type: 'record', record: RECORDS[6] as RunRecord}]);
    expect(runPhase(state).kind).toBe('stopping');
    expect(requests.some(request => request.kind === 'arm-bound')).toBe(true);
  });

  test('the ended banner names the round to resume from only when resumable', () => {
    const stopped = fold(initial(), [{type: 'record', record: RECORDS[7] as RunRecord}]).state;
    expect(endedView(stopped)).toMatchObject({title: 'Stopped', resume: null});
    const failed = fold(initial(), [{type: 'record', record: RECORDS[8] as RunRecord}]).state;
    expect(endedView(failed)).toMatchObject({title: 'Failed', resume: 'Resume from round 2'});
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
    expect(shown.tab).toBe('logs');
    expect(streamLines(shown, 's5')).toEqual([{text: '16:30:51 INFO run started', stream: 'out'}]);
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
      {type: 'run-data', data: {kind: 'events', events: [span]}},
    ]).state;
    const ended = fold(watched, [{type: 'record', record: RECORDS[8] as RunRecord}]);
    expect(ended.requests.some(request => request.kind === 'read-history')).toBe(false);
    expect(ended.state.run).toBe(watched.run);
    expect(ended.state.history).toEqual({kind: 'read'});
  });

  test('an ended run reads its history page by page into the run, and the tabs see its spans', () => {
    const span = (sequence: number, id: string, type: string): RunEvent =>
      ({
        sequence,
        type,
        timestamp: '2026-10-15T16:30:51Z',
        data: {span: {id}},
      }) as unknown as RunEvent;
    const ended = fold(initial(), [{type: 'record', record: RECORDS[8] as RunRecord}]);
    const first = ended.requests.find(request => request.kind === 'read-history');
    expect(first).toMatchObject({from: 0, max: HISTORY_PAGE_EVENTS});
    if (first?.kind !== 'read-history') throw new Error('no history asked');
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

describe('the Work tab filter', () => {
  test('changes only the work filter and asks for nothing', () => {
    const state = initial();
    for (const action of [
      {type: 'work-query', query: 'ring'},
      {type: 'work-outcome', outcome: 'rejected'},
    ] as const) {
      const step = runPageStep(state, action);
      expect(step.requests).toEqual([]);
      expect({...step.state, work: state.work}).toEqual(state);
    }
    const both = fold(state, [
      {type: 'work-query', query: 'ring'},
      {type: 'work-outcome', outcome: 'rejected'},
    ]);
    expect(both.state.work).toEqual({query: 'ring', outcome: 'rejected'});
  });
});
