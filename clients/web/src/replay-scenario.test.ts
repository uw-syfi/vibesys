import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {loadReplayScenario, parseReplayScenario, type ReplayScenario} from './index.js';

const fixturePath = resolve(import.meta.dir, '../dev/fixtures/trajectory-replay.json');
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8')) as unknown;

test('parses the rich replay and preserves its public campaign relationships', () => {
  const scenario = parseReplayScenario(fixture);

  expect(scenario.objective.target).toBeNull();
  expect(scenario.benchmarkVersions.map(version => version.id)).toEqual(['v4', 'v5']);
  expect(scenario.benchmarkVersionBoundary.afterSequence).toBe(12);
  expect(scenario.workstreams.length).toBeGreaterThanOrEqual(8);
  expect(new Set(scenario.workstreams.map(workstream => workstream.outcome))).toEqual(
    new Set(['accepted', 'rejected']),
  );
  expect(scenario.measurements.length).toBeGreaterThanOrEqual(12);
  expect(scenario.trajectories.every(trajectory => trajectory.turns.length > 0)).toBe(true);
  expect(
    scenario.trajectories
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
  (invalid.objective as Record<string, unknown>)['shadowTarget'] = 1249.317;

  expect(() => parseReplayScenario(invalid)).toThrow('$.objective.shadowTarget: unknown key');
});

test('rejects measurements attributed to the wrong benchmark side of the boundary', () => {
  const invalid = cloneFixture();
  const measurements = invalid.measurements as Array<Record<string, unknown>>;
  const afterBoundary = measurements.find(
    measurement =>
      Number(measurement['sequence']) >
      parseReplayScenario(fixture).benchmarkVersionBoundary.afterSequence,
  );
  if (afterBoundary === undefined)
    throw new Error('Fixture has no measurement after the version boundary');
  afterBoundary['benchmarkVersion'] = 'v4';

  expect(() => parseReplayScenario(invalid)).toThrow('expected v5 at sequence 13');
});

test('rejects a workstream link to an unknown agent before returning typed data', () => {
  const invalid = cloneFixture();
  const measurements = invalid.measurements as Array<Record<string, unknown>>;
  const firstMeasurement = measurements[0];
  if (firstMeasurement === undefined) throw new Error('Fixture has no measurements');
  firstMeasurement['triggeredByAgentId'] = 'missing-agent';

  expect(() => parseReplayScenario(invalid)).toThrow(
    '$.measurements[0].triggeredByAgentId: unknown agent',
  );
});

test('loads JSON through the injected fetch boundary and validates it', async () => {
  let requestedUrl = '';
  const loaded = await loadReplayScenario('/scenario.json', async url => {
    requestedUrl = url;
    return new Response(JSON.stringify(fixture));
  });

  expect(requestedUrl).toBe('/scenario.json');
  expect(loaded.id).toBe('qwen35-397b-mi300a-trajectory');
});

test('reports fixture HTTP failures without parsing a body', async () => {
  await expect(
    loadReplayScenario('/scenario.json', async () => new Response('unavailable', {status: 503})),
  ).rejects.toThrow('Replay scenario request failed with 503');
});

function cloneFixture(): ReplayScenario {
  return JSON.parse(JSON.stringify(fixture)) as ReplayScenario;
}
