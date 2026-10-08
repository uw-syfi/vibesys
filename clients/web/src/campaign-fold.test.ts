import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {
  foldCampaignFrame,
  foldFrames,
  foldStateToRecord,
  initialFoldState,
  latestMoment,
} from './campaign-fold.js';
import type {CampaignFrame, CampaignHeader, FrameMeasurement} from './campaign-frames.js';
import {framesFromRecord} from './campaign-replay.js';
import {type CampaignRecord, parseReplayScenario} from './replay-scenario.js';

const fixturePath = resolve(import.meta.dir, '../dev/fixtures/trajectory-replay.json');
const record: CampaignRecord = parseReplayScenario(
  JSON.parse(readFileSync(fixturePath, 'utf8')) as unknown,
);

/** Order-independent comparison: the fold keys by id, so array order is not
 * part of the contract. Measurements are compared in sequence order. */
function normalize(value: CampaignRecord): CampaignRecord {
  return {
    ...value,
    workstreams: [...value.workstreams].sort((a, b) => a.id.localeCompare(b.id)),
    agents: [...value.agents].sort((a, b) => a.id.localeCompare(b.id)),
    measurements: [...value.measurements].sort((a, b) => a.sequence - b.sequence),
  };
}

const header: CampaignHeader = {
  id: 'c',
  title: 't',
  summary: 's',
  provenance: 'p',
  objective: {
    title: 'o',
    statement: 'st',
    target: null,
    constraints: [],
    gates: [],
    metrics: [
      {
        id: 'goodput',
        name: 'Goodput',
        unit: 'tok/s',
        direction: 'maximize',
        description: 'd',
        benchmarkVersions: ['v5'],
      },
    ],
  },
  benchmarkVersions: [],
  benchmarkVersionBoundary: {fromVersion: 'v5', toVersion: 'v6', afterSequence: 0, reason: 'r'},
  trajectories: [],
};

function measurement(sequence: number, id: string, value = sequence * 10): FrameMeasurement {
  return {
    id,
    sequence,
    timestamp: `2026-01-01T00:00:${String(sequence).padStart(2, '0')}Z`,
    workstreamId: 'ws-1',
    label: `m${sequence}`,
    measurementKind: 'candidate',
    sourceOrder: null,
    triggeredByAgentId: null,
    runnerAgentId: null,
    benchmarkVersion: 'v5',
    values: [{metricId: 'goodput', value, unit: 'tok/s'}],
    gates: [],
    disposition: 'accepted',
  };
}

const initFrame: CampaignFrame = {kind: 'campaign-init', seq: 1, header};

test('folding a replayed record reconstructs the original record', () => {
  const frames = framesFromRecord(record);
  const reconstructed = foldStateToRecord(foldFrames(frames), latestMoment(foldFrames(frames)));
  expect(reconstructed).not.toBeNull();
  expect(normalize(reconstructed as CampaignRecord)).toEqual(normalize(record));
});

test('the reconstruction does not depend on the projection clock once every workstream is settled', () => {
  const state = foldFrames(framesFromRecord(record));
  expect(normalize(foldStateToRecord(state, 0) as CampaignRecord)).toEqual(
    normalize(foldStateToRecord(state, Date.parse('2200-06-01T00:00:00Z')) as CampaignRecord),
  );
});

test('replaying the log again is idempotent', () => {
  const frames = framesFromRecord(record);
  const once = foldFrames(frames);
  const twice = foldFrames([...frames, ...frames]);
  expect(twice.seq).toBe(once.seq);
  expect(normalize(foldStateToRecord(twice, 0) as CampaignRecord)).toEqual(
    normalize(foldStateToRecord(once, 0) as CampaignRecord),
  );
});

test('measurements are projected in sequence order regardless of arrival order', () => {
  const frames: CampaignFrame[] = [
    initFrame,
    {kind: 'measurement', seq: 2, measurement: measurement(3, 'm3')},
    {kind: 'measurement', seq: 3, measurement: measurement(1, 'm1')},
    {kind: 'measurement', seq: 4, measurement: measurement(2, 'm2')},
  ];
  const projected = foldStateToRecord(foldFrames(frames), 0) as CampaignRecord;
  expect(projected.measurements.map(item => item.sequence)).toEqual([1, 2, 3]);
});

test('a repeated measurement id upserts rather than duplicates', () => {
  const frames: CampaignFrame[] = [
    initFrame,
    {kind: 'measurement', seq: 2, measurement: measurement(5, 'm', 100)},
    {kind: 'measurement', seq: 3, measurement: measurement(5, 'm', 250)},
  ];
  const projected = foldStateToRecord(foldFrames(frames), 0) as CampaignRecord;
  expect(projected.measurements).toHaveLength(1);
  expect(projected.measurements[0]?.values[0]?.value).toBe(250);
});

test('token frames carry the cumulative total per workstream', () => {
  const state = foldFrames([
    initFrame,
    {kind: 'tokens', seq: 2, workstreamId: 'ws-1', tokens: 100},
    {kind: 'tokens', seq: 3, workstreamId: 'ws-1', tokens: 250},
    {kind: 'tokens', seq: 4, workstreamId: 'ws-2', tokens: 40},
  ]);
  expect(Object.fromEntries(state.tokenSpend)).toEqual({'ws-1': 250, 'ws-2': 40});
});

test('the run status is active until a completed status frame arrives', () => {
  expect(initialFoldState().status).toBe('active');
  const active = foldCampaignFrame(initialFoldState(), initFrame);
  expect(active.status).toBe('active');
  const done = foldCampaignFrame(active, {kind: 'status', seq: 2, status: 'completed'});
  expect(done.status).toBe('completed');
});

test('an in-flight workstream is projected to end just after the current moment', () => {
  const at = Date.parse('2026-01-01T00:00:05Z');
  const frames: CampaignFrame[] = [
    initFrame,
    {
      kind: 'workstream-upsert',
      seq: 2,
      workstream: {
        id: 'ws-1',
        title: 'Live one',
        hypothesis: 'h',
        startedAt: '2026-01-01T00:00:00Z',
        firstSequence: 1,
        phase: 'implementing',
        active: true,
        finishedAt: null,
        lastSequence: null,
        outcome: null,
        outcomeSummary: '',
      },
    },
    {kind: 'measurement', seq: 3, measurement: measurement(5, 'm5')},
  ];
  const now = latestMoment(foldFrames(frames));
  const projected = foldStateToRecord(foldFrames(frames), now) as CampaignRecord;
  const workstream = projected.workstreams[0];
  expect(workstream).toBeDefined();
  // Ends after "now" so the dashboard's cursor-vs-finishedAt test renders it active.
  expect(Date.parse(workstream?.finishedAt ?? '')).toBeGreaterThan(at);
});

test('latestMoment prefers measurements, falls back to workstream starts, then zero', () => {
  expect(latestMoment(initialFoldState())).toBe(0);
  const withMeasurement = foldFrames([
    initFrame,
    {kind: 'measurement', seq: 2, measurement: measurement(9, 'm9')},
  ]);
  expect(latestMoment(withMeasurement)).toBe(Date.parse('2026-01-01T00:00:09Z'));
});

test('the header must arrive before a record can be projected', () => {
  expect(foldStateToRecord(initialFoldState(), 0)).toBeNull();
});
