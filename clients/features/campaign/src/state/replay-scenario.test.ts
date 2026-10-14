import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {type CampaignRecord, parseReplayScenario} from './index.js';

const fixturePath = resolve(
  import.meta.dir,
  '../../../../apps/web/dev/fixtures/trajectory-replay.json',
);
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8')) as unknown;

test('parses the source-backed campaign record and preserves its measurements', () => {
  const campaign: CampaignRecord = parseReplayScenario(fixture);

  expect(campaign.id).toBe('qwen35-397b-campaign');
  expect(campaign.benchmarkVersions.map(version => version.id)).toEqual(['v5', 'v6']);
  expect(campaign.benchmarkVersionBoundary).toMatchObject({
    fromVersion: 'v5',
    toVersion: 'v6',
    afterSequence: 74,
  });
  expect(campaign.workstreams.length).toBeGreaterThanOrEqual(20);
  expect(new Set(campaign.workstreams.map(workstream => workstream.outcome))).toEqual(
    new Set(['accepted', 'rejected']),
  );
  expect(campaign.measurements).toHaveLength(111);
  expect(campaign.measurements.map(measurement => measurement.sequence)).toEqual(
    Array.from({length: 111}, (_, index) => index + 1),
  );
  expect(new Set(campaign.measurements.map(measurement => measurement.measurementKind))).toEqual(
    new Set(['official', 'candidate', 'control', 'diagnostic', 'quick']),
  );
  expect(
    campaign.measurements.filter(measurement => measurement.sourceOrder !== null),
  ).toHaveLength(82);
  expect(campaign.objective.target).toEqual({metricId: 'goodput', value: 2000, unit: 'tok/s'});
  const plottedMeasurements = campaign.measurements.filter(
    measurement => measurement.sourceOrder !== null,
  );
  expect(plottedMeasurements.map(measurement => measurement.sourceOrder)).toContain(48);
  expect(plottedMeasurements.map(measurement => measurement.sourceOrder)).toContain(142);
  expect(
    plottedMeasurements.flatMap(measurement => measurement.values.map(value => value.value)),
  ).toContain(94.7);
  expect(
    plottedMeasurements.flatMap(measurement => measurement.values.map(value => value.value)),
  ).toContain(1154.4770391356592);
  expect(
    campaign.measurements.flatMap(measurement => measurement.values.map(value => value.value)),
  ).toContain(2242.4);
  expect(
    campaign.measurements.every(
      measurement => measurement.triggeredByAgentId === null && measurement.runnerAgentId === null,
    ),
  ).toBe(true);
  expect(JSON.stringify(campaign.measurements).toLowerCase()).not.toContain('sglang');
  expect(campaign.trajectories.every(trajectory => trajectory.turns.length > 0)).toBe(true);
  expect(
    campaign.trajectories
      .flatMap(trajectory => trajectory.turns)
      .some(
        turn =>
          turn.messages.some(message => message.kind === 'tool_call') &&
          turn.messages.some(message => message.kind === 'tool_result'),
      ),
  ).toBe(true);
});

test('rejects unknown keys at the fixture boundary with their path', () => {
  const invalid = cloneFixture();
  (invalid['objective'] as Record<string, unknown>)['shadowTarget'] = 1249.317;

  expect(() => parseReplayScenario(invalid)).toThrow('$.objective.shadowTarget: unknown key');
});

test('legacy schema-v1 records without architecture remain valid', () => {
  const legacy = cloneFixture();
  delete legacy['architecture'];

  expect(parseReplayScenario(legacy).architecture).toBeNull();
});

test('architecture observations reference a campaign measurement at its exact sequence', () => {
  const unknownMeasurement = cloneFixture();
  const unknownArchitecture = unknownMeasurement['architecture'] as Record<string, unknown>;
  unknownArchitecture['observations'] = [architectureObservation('missing-measurement', 1)];
  expect(() => parseReplayScenario(unknownMeasurement)).toThrow(
    '$.architecture.observations[0].measurement_id: unknown measurement missing-measurement',
  );

  const wrongSequence = cloneFixture();
  const measurements = wrongSequence['measurements'] as Record<string, unknown>[];
  const first = measurements[0];
  if (first === undefined) throw new Error('Fixture has no measurements');
  const architecture = wrongSequence['architecture'] as Record<string, unknown>;
  architecture['observations'] = [
    architectureObservation(String(first['id']), Number(first['sequence']) + 1),
  ];
  expect(() => parseReplayScenario(wrongSequence)).toThrow(
    `$.architecture.observations[0].sequence: expected ${first['sequence']} for measurement ${first['id']}`,
  );
});

test('rejects measurements attributed to the wrong benchmark side of the boundary', () => {
  const invalid = cloneFixture();
  const campaign = parseReplayScenario(fixture);
  const measurements = invalid['measurements'] as Array<Record<string, unknown>>;
  const afterBoundary = measurements.find(
    measurement =>
      Number(measurement['sequence']) > campaign.benchmarkVersionBoundary.afterSequence,
  );
  if (afterBoundary === undefined)
    throw new Error('Fixture has no measurement after the version boundary');
  afterBoundary['benchmarkVersion'] = campaign.benchmarkVersionBoundary.fromVersion;

  expect(() => parseReplayScenario(invalid)).toThrow(
    `expected ${campaign.benchmarkVersionBoundary.toVersion} at sequence ${afterBoundary['sequence']}`,
  );
});

test('rejects a workstream link to an unknown agent before returning typed data', () => {
  const invalid = cloneFixture();
  const measurements = invalid['measurements'] as Array<Record<string, unknown>>;
  const firstMeasurement = measurements[0];
  if (firstMeasurement === undefined) throw new Error('Fixture has no measurements');
  firstMeasurement['triggeredByAgentId'] = 'missing-agent';

  expect(() => parseReplayScenario(invalid)).toThrow(
    '$.measurements[0].triggeredByAgentId: unknown agent',
  );
});

function cloneFixture(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(fixture)) as Record<string, unknown>;
}

function architectureObservation(measurementId: string, sequence: number): Record<string, unknown> {
  return {
    measurement_id: measurementId,
    sequence,
    label: 'Architecture capture',
    changes: [],
    attributions: [],
  };
}
