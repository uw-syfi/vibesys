import {expect, test} from 'bun:test';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostRun} from '@vibesys/hosts';
import {emptyLogView, type LogView} from '@vibesys/logs';
import {Seeded} from '@vibesys/store/testing';
import {type RunPageState, runPageInitial} from './run-page.js';
import {paneLines, scopedLogs} from './scoped-logs.js';
import {idea} from './timeline-fixtures.js';

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

function pageState(
  streams: readonly LogStream[],
  selected: string | null = null,
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
    selected,
    views,
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

function viewOf(lines: readonly string[]): LogView {
  return {
    ...emptyLogView(),
    lines: lines.map(text => ({text, runs: [{length: text.length, pipe: 'stdout'}]})),
  };
}

function narration(message: string): string {
  const at = '2026-10-15T16:36:20+00:00';
  return JSON.stringify({at, level: 'INFO', logger: 'run', thread: 'main', message});
}

test('the narration is listed once: its label already says what the owner would', () => {
  const logs = scopedLogs(pageState(restarted(1)), {kind: 'run'});
  const leads = Object.fromEntries(logs.streams.map(item => [item.id, item.lead]));
  expect(leads['narration']).toBeNull();
  expect(leads['gate']).toBe('Gates');
});

test('developer boot timing stays out of the narration unless diagnostics are asked for', () => {
  const lines = [
    narration('boot span run_preamble: 100ms'),
    narration('started on host-b'),
    'boot span context: 431ms',
    narration('boot span context.project_open: 12ms (raised ValueError)'),
    'round 2 started',
  ];
  const state = pageState(restarted(1), null, {
    narration: viewOf(lines),
    gate: viewOf(['boot span build: 5ms']),
  });
  expect(paneLines(state, 'narration').map(line => line.text)).toEqual([
    '16:36:20 INFO started on host-b',
    'round 2 started',
  ]);
  expect(paneLines(state, 'narration', true)).toHaveLength(5);
  // Only the narration is the runtime's own voice: a gate's output is shown as written.
  expect(paneLines(state, 'gate').map(line => line.text)).toEqual(['boot span build: 5ms']);
  const last = scopedLogs(state, {kind: 'run'}).streams.find(item => item.id === 'narration');
  expect(last?.lastLine).toBe('round 2 started');
});

test('an empty Logs face names its scope and speaks of what a finished run did, not of yet', () => {
  const live = pageState([]);
  const ended: RunPageState = {...live, record: {kind: 'historical', status: 'completed'}};
  const scopes = [
    ['run', {kind: 'run'}],
    ['idea', {kind: 'idea', idea: idea('a', 'passed', 0, 10)}],
  ] as const;
  for (const [subject, scope] of scopes) {
    expect(scopedLogs(live, scope).notice).toMatch(/not written any logs yet/);
    expect(scopedLogs(ended, scope).notice).toBe(`This ${subject} wrote no logs.`);
    // A scope never says "the run" about something narrower.
    if (subject !== 'run') expect(scopedLogs(live, scope).notice).not.toMatch(/\brun\b/i);
  }
});
