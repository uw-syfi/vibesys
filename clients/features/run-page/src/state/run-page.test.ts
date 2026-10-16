import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun} from '@vibesys/hosts';
import {emptyLogView, foldChunk} from '@vibesys/logs';
import {checkModel, type Seeded} from '@vibesys/store/testing';
import {
  endedView,
  FORCE_STOP_BOUND_MS,
  followedStreams,
  headerView,
  logGroups,
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

const USER_ACTIONS: readonly RunPageAction[] = [
  {type: 'ask-stop'},
  {type: 'cancel-stop'},
  {type: 'confirm-stop'},
  {type: 'force-stop'},
  {type: 'stop-failed'},
  {type: 'pause'},
  {type: 'resume'},
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

/** What the attached run's session might report, current or late. */
function liveAction(random: Seeded, issued: readonly number[]): RunPageAction {
  switch (random.int(4)) {
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
      return {
        type: 'run-data',
        data: {
          kind: random.pick(['events', 'rebootstrap'] as const),
          events: Array.from({length: random.int(4)}, () => runEvent(random, random.int(30))),
        },
      };
    default:
      return {type: 'view', view: random.bool() ? {at: 'live'} : {at: random.int(30)}};
  }
}

function randomAction(random: Seeded, issued: readonly number[]): RunPageAction {
  switch (random.int(8)) {
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
    default:
      return random.pick(USER_ACTIONS);
  }
}

function issues(request: RunPageRequest): number | null {
  return request.kind === 'arm-bound' || request.kind === 'list-logs' || request.kind === 'attach'
    ? request.token
    : null;
}

function awaited(state: RunPageState) {
  return {
    bound: state.stop.kind === 'stopping' ? state.stop.bound : null,
    logs: state.logs.kind === 'listing' ? state.logs.token : null,
    attach: state.link.kind === 'attaching' ? state.link.token : null,
  };
}

function reply(action: RunPageAction) {
  if (action.type === 'bound-elapsed') return {slot: 'bound', token: action.token};
  if (action.type === 'streams') return {slot: 'logs', token: action.token};
  if (action.type === 'attached') return {slot: 'attach', token: action.token};
  return null;
}

/** Why `request` was not on offer in `before`, or null when it was. */
function notOffered(before: RunPageState, request: RunPageRequest): string | null {
  const header = headerView(before);
  switch (request.kind) {
    case 'pause':
      return header.pause.shown && header.pause.disabledReason === null
        ? null
        : 'pause was sent while not offered';
    case 'resume':
      return header.resume.shown ? null : 'resume was sent while not offered';
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
