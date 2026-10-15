import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {
  type CampaignMetricAnalytics,
  type CampaignRecord,
  parseReplayScenario,
  projectCampaignMetric,
} from './index.js';

const fixture = parseReplayScenario(
  JSON.parse(
    readFileSync(
      resolve(import.meta.dir, '../../../../apps/web/dev/fixtures/trajectory-replay.json'),
      'utf8',
    ),
  ),
);

function metricCampaign(
  direction: 'maximize' | 'minimize',
  observations: ReadonlyArray<{
    readonly disposition: CampaignRecord['measurements'][number]['disposition'];
    readonly value: number;
  }>,
): CampaignRecord {
  const metric = fixture.objective.metrics[0];
  if (metric === undefined) throw new Error('campaign fixture must define a metric');
  return {
    ...fixture,
    objective: {
      ...fixture.objective,
      metrics: [{...metric, direction}],
    },
    measurements: observations.map((observation, index) => {
      const source = fixture.measurements[index];
      if (source === undefined) throw new Error('campaign fixture has too few measurements');
      return {
        ...source,
        disposition: observation.disposition,
        values: [{metricId: metric.id, value: observation.value, unit: metric.unit}],
      };
    }),
  };
}

test.each([
  {
    direction: 'maximize' as const,
    observations: [
      {disposition: 'rejected' as const, value: 100},
      {disposition: 'inconclusive' as const, value: 90},
      {disposition: 'accepted' as const, value: 10},
      {disposition: 'rejected' as const, value: 200},
      {disposition: 'inconclusive' as const, value: 300},
      {disposition: 'accepted' as const, value: 8},
      {disposition: 'accepted' as const, value: 20},
    ],
    expectedValues: [10, 20],
  },
  {
    direction: 'minimize' as const,
    observations: [
      {disposition: 'rejected' as const, value: 1},
      {disposition: 'inconclusive' as const, value: 2},
      {disposition: 'accepted' as const, value: 30},
      {disposition: 'rejected' as const, value: 0},
      {disposition: 'inconclusive' as const, value: -1},
      {disposition: 'accepted' as const, value: 40},
      {disposition: 'accepted' as const, value: 20},
    ],
    expectedValues: [30, 20],
  },
])('$direction incumbents use accepted observations only', ({
  direction,
  observations,
  expectedValues,
}) => {
  const record = metricCampaign(direction, observations);
  const metricId = record.objective.metrics[0]?.id ?? '';

  expect(projectCampaignMetric(record, metricId, 1)).toEqual({
    incumbent: null,
    runningBestSteps: [],
  });
  const analytics = projectCampaignMetric(record, metricId, record.measurements.length - 1);
  expect(analytics.runningBestSteps.map(step => step.value)).toEqual([...expectedValues]);
  expect(analytics.runningBestSteps.map(step => step.sequence)).toEqual([3, 7]);
  expect(analytics.incumbent).toEqual(analytics.runningBestSteps.at(-1) ?? null);
});

test('fixture incumbents do not advance at the reported rejected and inconclusive prefixes', () => {
  const metricId = fixture.objective.target?.metricId ?? '';
  const expected = new Map([
    [2, {sequence: 1, value: 12.8}],
    [6, {sequence: 3, value: 55.6}],
    [18, {sequence: 3, value: 55.6}],
    [74, {sequence: 67, value: 784.9166}],
  ]);

  for (const [throughSequence, incumbent] of expected) {
    const throughIndex = fixture.measurements.findIndex(
      measurement => measurement.sequence === throughSequence,
    );
    if (throughIndex === -1) throw new Error(`fixture measurement ${throughSequence} missing`);
    const analytics = projectCampaignMetric(fixture, metricId, throughIndex);
    expect(analytics.incumbent).toMatchObject(incumbent);
    expect(analytics.incumbent).toEqual(analytics.runningBestSteps.at(-1) ?? null);
  }
});

test('the incumbent and final running-best step agree at every fixture prefix', () => {
  const metricId = fixture.objective.target?.metricId ?? '';
  let previousAcceptedIncumbent: CampaignMetricAnalytics['incumbent'] = null;
  for (const [measurementIndex, measurement] of fixture.measurements.entries()) {
    const analytics = projectCampaignMetric(fixture, metricId, measurementIndex);
    expect(analytics.incumbent).toEqual(analytics.runningBestSteps.at(-1) ?? null);
    if (measurement.disposition !== 'accepted')
      expect(analytics.incumbent).toEqual(previousAcceptedIncumbent);
    previousAcceptedIncumbent = analytics.incumbent;
  }
});

test.each([
  {
    direction: 'maximize' as const,
    baseline: 10,
    ineligible: 100,
    future: 20,
  },
  {
    direction: 'minimize' as const,
    baseline: 30,
    ineligible: 1,
    future: 20,
  },
])('$direction history is closed over the selected index when sequences repeat', ({
  direction,
  baseline,
  ineligible,
  future,
}) => {
  const source = metricCampaign(direction, [
    {disposition: 'accepted', value: baseline},
    {disposition: 'rejected', value: ineligible},
    {disposition: 'accepted', value: future},
  ]);
  const record = {
    ...source,
    measurements: source.measurements.map((measurement, index) => ({
      ...measurement,
      sequence: index === 0 ? 1 : 2,
    })),
  };
  const metricId = record.objective.metrics[0]?.id ?? '';

  expect(projectCampaignMetric(record, metricId, 1).incumbent?.value).toBe(baseline);
  expect(projectCampaignMetric(record, metricId, 1).runningBestSteps).toHaveLength(1);
  expect(projectCampaignMetric(record, metricId, 2).incumbent?.value).toBe(future);
});
