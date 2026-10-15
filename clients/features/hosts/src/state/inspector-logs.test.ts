import {describe, expect, test} from 'bun:test';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {LogListOutcome} from '@vibesys/logs';
import {checkMachine} from '@vibesys/machine/testing';
import {
  groupedStreams,
  type InspectorLogsSnapshot,
  inspectorLogsMachine,
  inspectorLogsNotice,
  inspectorLogsView,
  inspectorStreams,
  shownStream,
} from './inspector-logs.js';

function stream(id: string, owner: LogStream['owner'], outcome: LogStream['outcome']): LogStream {
  return {id, owner, command: 'c', retained_start: 0, size: 0, outcome};
}

const STREAMS: readonly LogStream[] = [
  stream('000001', {kind: 'startup_step', stage: 'building_image'}, 'ended'),
  stream('000002', {kind: 'run_narration'}, null),
  stream(
    '000003',
    {kind: 'gate_stage', execution_id: 'exec-7', gate: 'accuracy', attempt: 2},
    'failed',
  ),
  stream('000004', {kind: 'tool_build', name: 'nsys'}, 'abandoned'),
  stream('000005', {kind: 'agent_invocation', invocation_id: 'inv-1'}, 'ended'),
  stream('000006', {kind: 'slurm_job', execution_id: 'exec-7', job_id: '4242'}, null),
  stream('000007', {kind: 'from_a_newer_writer'}, null),
];

const TARGET = {host: 'local' as const, instanceId: '0123456789ab'};

const OUTCOMES: readonly LogListOutcome[] = [
  {ok: true, instanceId: TARGET.instanceId, streams: STREAMS},
  {ok: true, instanceId: TARGET.instanceId, streams: STREAMS.slice(0, 1)},
  {ok: true, instanceId: TARGET.instanceId, streams: []},
  {ok: false, failure: {code: 'transport', message: 'dropped'}},
  {ok: false, failure: {code: 'feature_not_negotiated', message: 'no logs'}},
];

describe('inspector log streams', () => {
  test('are labelled by what they are for, with their status, grouped by owner kind', () => {
    const groups = groupedStreams(inspectorStreams(STREAMS));
    expect(
      groups.map(group => [
        group.label,
        group.streams.map(entry => `${entry.label} (${entry.status})`),
      ]),
    ).toEqual([
      ['Run narration', ['Run narration (running)']],
      ['Startup', ['Startup / building image (ended)']],
      ['Gates', ['Gate accuracy (attempt 2), exec-7 (failed)']],
      ['Agent invocations', ['Agent invocation inv-1 (ended)']],
      ['Tool builds', ['Tool build / nsys (abandoned)']],
      ['Slurm jobs', ['Slurm job 4242 (running)']],
      ['Other', ['from a newer writer (running)']],
    ]);
    expect(
      inspectorStreams(STREAMS)
        .filter(entry => entry.narration)
        .map(entry => entry.id),
    ).toEqual(['000002']);
  });

  test("show the user's pick while it is listed, else the narration, else the newest", () => {
    const streams = inspectorStreams(STREAMS);
    const context = {
      target: TARGET,
      streams,
      chosen: null,
      followed: null,
      listFailure: null,
      retryMs: 1,
      follow: undefined,
    };
    expect(shownStream(context)).toBe('000002');
    expect(shownStream({...context, chosen: '000003'})).toBe('000003');
    expect(shownStream({...context, chosen: '999999'})).toBe('000002');
    expect(shownStream({...context, streams: streams.slice(2, 4)})).toBe('000004');
    expect(shownStream({...context, streams: []})).toBeNull();
  });
});

function followerProblems(snapshot: InspectorLogsSnapshot): string[] {
  if (!snapshot.matches({open: {follow: 'following'}})) return [];
  const problems: string[] = [];
  if (snapshot.context.target === null) problems.push('a follower runs with no run inspected');
  if (snapshot.context.followed !== shownStream(snapshot.context))
    problems.push('the follower reads a stream that is not on screen');
  return problems;
}

function closedProblems(snapshot: InspectorLogsSnapshot): string[] {
  if (!snapshot.matches('closed')) return [];
  const problems: string[] = [];
  if (snapshot.context.streams.length > 0) problems.push('a closed inspector still lists streams');
  if (inspectorLogsView(snapshot, null).open) problems.push('a closed inspector shows logs');
  return problems;
}

describe('inspectorLogsMachine', () => {
  test('model: work runs only while open, and the follower reads only the stream on screen', () => {
    checkMachine({
      machine: inspectorLogsMachine,
      input: {retryMs: 1000},
      event: (random, live) => {
        const settle = live.operations
          .filter(operation => operation === 'list')
          .flatMap(operation => [
            live.done(operation, random.pick(OUTCOMES)),
            live.error(operation, new Error('bridge failed')),
          ]);
        const fire = live.delays.map(delay => live.fire(delay));
        return random.pick([
          {type: 'open', target: random.pick([TARGET, {...TARGET, host: 'ssh:gpu' as const}])},
          {type: 'close'},
          {type: 'refresh'},
          {type: 'select', streamId: random.pick(['000001', '000003', '999999'])},
          ...settle,
          ...settle,
          ...fire,
        ]);
      },
      invariants: snapshot => [...followerProblems(snapshot), ...closedProblems(snapshot)],
    });
  });

  test('the notice says unsupported instead of an error, and that an ended run recorded none', () => {
    const view = {
      open: true,
      listing: 'unsupported' as const,
      listFailure: {code: 'feature_not_negotiated' as const, message: 'no logs'},
      groups: [],
      shown: null,
      lines: [],
      earlier: false,
      complete: false,
      readFailure: null,
    };
    expect(inspectorLogsNotice(view)).toContain('does not serve logs');
    expect(inspectorLogsNotice({...view, listing: 'loaded', listFailure: null})).toBe(
      'This run recorded no logs.',
    );
  });
});
