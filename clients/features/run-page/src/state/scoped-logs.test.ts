import {expect, test} from 'bun:test';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun} from '@vibesys/hosts';
import {Seeded} from '@vibesys/store/testing';
import {type RunPageState, runPageInitial} from './index.js';
import {scopedLogs} from './scoped-logs.js';

type Owner = LogStream['owner'];

const STAGES = ['building_image', 'starting_container'] as const;

function stream(id: string, owner: Owner, live: boolean): LogStream {
  return {
    id,
    owner,
    command: 'cmd',
    retained_start: 0,
    size: 0,
    outcome: live ? null : 'ended',
  };
}

function pageState(streams: readonly LogStream[], selected: string | null = null): RunPageState {
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
      identity: {host: 'ssh:chelan4', runId: 'run'},
      title: {project: 'queue-rs', task: 'mpmc'},
      hostLabel: 'chelan4',
      unavailable: {},
    }),
    record: {kind: 'live', run},
    logs: {kind: 'listed', instanceId: run.id, streams},
    selected,
  };
}

/** Startup steps repeated by restarts, then the work the run did since. */
function restarted(repeats: number): LogStream[] {
  const startup = Array.from({length: repeats}, (_, index) =>
    STAGES.map(stage => stream(`${stage}-${index}`, {kind: 'startup_step', stage}, false)),
  ).flat();
  return [
    ...startup,
    stream('gate', {kind: 'gate_stage', gate: 'build', execution_id: 'w1', attempt: 0}, false),
    stream('narration', {kind: 'run_narration'}, true),
  ];
}

test('the repeats of a startup step fold into one row with a count, after the work', () => {
  const logs = scopedLogs(pageState(restarted(9)), {kind: 'run'});
  expect(
    logs.rows.map(row => (row.kind === 'fold' ? `${row.label} ×${row.count}` : row.stream.id)),
  ).toEqual([
    'narration',
    'gate',
    'Building the agent image ×9',
    'Starting the agent container ×9',
  ]);
});

test('a fold opens while the shown stream is one of its streams, and lists them all', () => {
  const logs = scopedLogs(pageState(restarted(3), 'building_image-1'), {kind: 'run'});
  const ids = logs.rows.map(row => (row.kind === 'fold' ? row.id : row.stream.id));
  expect(ids).toEqual([
    'narration',
    'gate',
    'fold building_image',
    'building_image-0',
    'building_image-1',
    'building_image-2',
    'fold starting_container',
  ]);
  const fold = logs.rows.find(row => row.kind === 'fold');
  expect(fold?.kind === 'fold' && fold.latest).toBe('building_image-2');
});

test('a startup step that ran once stays a plain row', () => {
  const logs = scopedLogs(pageState(restarted(1)), {kind: 'run'});
  expect(logs.rows.every(row => row.kind === 'stream')).toBe(true);
});

test('startup is last, and the groups that write or wrote last come first, for any listing', () => {
  const random = new Seeded(11);
  const owners: readonly Owner[] = [
    {kind: 'startup_step', stage: 'building_image'},
    {kind: 'startup_step', stage: 'starting_container'},
    {kind: 'gate_stage', gate: 'build', execution_id: 'e', attempt: 0},
    {kind: 'tool_build', name: 'perf'},
    {kind: 'run_narration'},
  ];
  for (let round = 0; round < 50; round++) {
    const listing = Array.from({length: 1 + random.int(14)}, (_, index) =>
      stream(`s${index}`, random.pick(owners), random.bool()),
    );
    const logs = scopedLogs(pageState(listing), {kind: 'run'});
    // Every stream is still there, and no startup stream comes before another owner's.
    expect(logs.streams.map(item => item.id).sort()).toEqual(listing.map(item => item.id).sort());
    const firstStartup = logs.streams.findIndex(item => item.owner === 'Startup');
    if (firstStartup >= 0) {
      expect(logs.streams.slice(firstStartup).every(item => item.owner === 'Startup')).toBe(true);
    }
    // A group with a running stream never comes after one without (startup aside).
    const others = logs.streams.filter(item => item.owner !== 'Startup');
    const owner = (name: string) => others.filter(item => item.owner === name);
    const names = [...new Set(others.map(item => item.owner))];
    const writing = names.map(name => owner(name).some(item => item.live));
    expect(writing).toEqual([...writing].sort((a, b) => Number(b) - Number(a)));
  }
});
