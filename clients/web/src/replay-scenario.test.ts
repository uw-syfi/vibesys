import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {loadReplayScenario, parseReplayScenario, type CampaignRecord} from './replay-scenario.js';

const fixturePath = resolve(import.meta.dir, '../dev/fixtures/trajectory-replay.json');
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

test('loads JSON through the injected fetch boundary and validates it', async () => {
  let requestedUrl = '';
  const loaded: CampaignRecord = await loadReplayScenario('/campaign.json', async url => {
    requestedUrl = url;
    return new Response(JSON.stringify(fixture));
  });

  expect(requestedUrl).toBe('/campaign.json');
  expect(loaded.id).toBe('qwen35-397b-campaign');
});

test('reports fixture HTTP failures without parsing a body', async () => {
  await expect(
    loadReplayScenario('/campaign.json', async () => new Response('unavailable', {status: 503})),
  ).rejects.toThrow('Replay scenario request failed with 503');
});

function cloneFixture(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(fixture)) as Record<string, unknown>;
}
