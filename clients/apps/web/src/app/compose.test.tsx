import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {CampaignDashboard, createCampaignHistoryStore} from '@vibesys/feature-campaign/renderer';
import {
  type CampaignRecord,
  framesFromRecord,
  parseReplayScenario,
} from '@vibesys/feature-campaign/state';
import {targetFromCapability} from '@vibesys/feature-gateway-connect/renderer';
import {renderToStaticMarkup} from 'react-dom/server';
import {createDemoApp, createLiveCampaignApp} from './compose.js';

const campaignRecord = parseReplayScenario(
  JSON.parse(
    readFileSync(resolve(import.meta.dir, '../../dev/fixtures/trajectory-replay.json'), 'utf8'),
  ),
);

async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn++) await Promise.resolve();
}

function renderCampaignAt(record: CampaignRecord, pointIndex: number): string {
  const store = createCampaignHistoryStore(record);
  store.dispatch({type: 'point', index: pointIndex});
  const {scenario, campaign} = store.getState();
  if (scenario === null || campaign === null) throw new Error('Campaign history did not project');
  try {
    return renderToStaticMarkup(<CampaignDashboard scenario={scenario} campaign={campaign} />);
  } finally {
    store.close();
  }
}

function trajectoryAnalytics(markup: string): {readonly axis: string; readonly changed: boolean} {
  const axis =
    /<div class="chart-y-axis chart-y-axis-left" aria-hidden="true">([\s\S]*?)<\/div>/.exec(
      markup,
    )?.[1];
  if (axis === undefined) throw new Error('Campaign trajectory axis did not render');
  return {axis, changed: markup.includes('Benchmark changed mid-run')};
}

test('renders an initial capability launch before its exchange settles', () => {
  const target = targetFromCapability(
    'http://127.0.0.1:5173/',
    'http://127.0.0.1:8765/?token=launch-secret',
  );
  if (target.bootstrapUrl === null) throw new Error('Expected a bootstrap target');
  const attempts: Array<{url: string; signal: AbortSignal}> = [];
  const app = createDemoApp(
    {
      page: 'http://127.0.0.1:5173/',
      initialGatewayTarget: {...target, bootstrapUrl: target.bootstrapUrl},
      onGatewayConnected: () => undefined,
    },
    {
      bootstrap: (url, signal) => {
        attempts.push({url, signal});
        return new Promise<string>(() => undefined);
      },
      loadCampaign: () => new Promise(() => undefined),
    },
  );

  const html = renderToStaticMarkup(app.view);

  expect(attempts).toHaveLength(1);
  expect(attempts[0]?.url).toBe('http://127.0.0.1:8765/?token=launch-secret');
  expect(attempts[0]?.signal).toBeInstanceOf(AbortSignal);
  expect(html).toContain('Connecting…');
  expect(html).toContain('>Cancel</button>');
  expect(html).toContain('role="status"');
  expect(html).toContain('aria-live="polite"');
  expect(html).toContain('aria-atomic="true"');
  expect(html).not.toContain('launch-secret');
  app.close();
  expect(attempts[0]?.signal.aborted).toBe(true);
});

test('campaign preview loads through its campaign service alone', async () => {
  const app = createDemoApp(
    {page: 'http://127.0.0.1:5173/', onGatewayConnected: () => undefined},
    {
      bootstrap: async () => 'unused',
      loadCampaign: async () => campaignRecord,
    },
  );

  await flush();
  const html = renderToStaticMarkup(app.view);
  expect(html).toContain('Qwen3.5-397B-A17B on 4x MI300A');
  expect(html).not.toContain('Replay failed to load');
  app.close();
});

test('the page owner releases a preview stream independently of React render lifetimes', () => {
  let active = false;
  const page = createLiveCampaignApp({
    subscribe: () => {
      active = true;
      return () => {
        active = false;
      };
    },
  });
  expect(active).toBe(true);
  renderToStaticMarkup(page.view);
  expect(active).toBe(true);
  page.close();
  expect(active).toBe(false);
});

test('live campaign replay with no usage omits the token column', () => {
  const frames = framesFromRecord(campaignRecord);
  const page = createLiveCampaignApp({
    subscribe: onFrame => {
      for (const frame of frames) onFrame(frame);
      return () => undefined;
    },
  });

  const html = renderToStaticMarkup(page.view);
  expect(html).toContain('Work history');
  expect(html).not.toContain('Sort by Tokens');
  page.close();
});

test('historical trajectory analytics depend only on measurements through the cursor', () => {
  let firstAnalytics: ReturnType<typeof trajectoryAnalytics> | undefined;
  let boundaryAnalytics: ReturnType<typeof trajectoryAnalytics> | undefined;
  for (let pointIndex = 0; pointIndex < campaignRecord.measurements.length; pointIndex++) {
    const completedMarkup = renderCampaignAt(campaignRecord, pointIndex);
    const prefixMarkup = renderCampaignAt(
      {...campaignRecord, measurements: campaignRecord.measurements.slice(0, pointIndex + 1)},
      pointIndex,
    );
    expect(trajectoryAnalytics(completedMarkup)).toEqual(trajectoryAnalytics(prefixMarkup));
    if (pointIndex === 0) {
      firstAnalytics = trajectoryAnalytics(completedMarkup);
      expect(completedMarkup).toContain('111 recorded');
      expect(completedMarkup).toContain('01 <i>/</i> 111');
    }
    if (pointIndex === 74) boundaryAnalytics = trajectoryAnalytics(completedMarkup);
  }
  expect(firstAnalytics?.axis).toContain('13.8 tok/s');
  expect(firstAnalytics?.changed).toBe(false);
  expect(boundaryAnalytics?.changed).toBe(true);
});

test('a future multi-metric suffix cannot change the implicit historical metric', () => {
  const latencyMetric = {
    id: 'latency',
    name: 'Latency',
    unit: 'ms',
    direction: 'minimize' as const,
    description: 'Request latency under the recorded benchmark contract.',
    benchmarkVersions: ['v5', 'v6'],
  };
  const lastIndex = campaignRecord.measurements.length - 1;
  const multiMetricRecord = parseReplayScenario({
    ...campaignRecord,
    objective: {
      ...campaignRecord.objective,
      metrics: [...campaignRecord.objective.metrics, latencyMetric],
    },
    benchmarkVersions: campaignRecord.benchmarkVersions.map(version => ({
      ...version,
      definitions: [
        ...version.definitions,
        {
          metricId: latencyMetric.id,
          unit: latencyMetric.unit,
          direction: latencyMetric.direction,
          description: latencyMetric.description,
        },
      ],
    })),
    measurements: campaignRecord.measurements.map((measurement, index) => ({
      ...measurement,
      values:
        index === 0
          ? [...measurement.values, {metricId: latencyMetric.id, value: 80, unit: 'ms'}]
          : index === lastIndex
            ? [{metricId: latencyMetric.id, value: 10, unit: 'ms'}, ...measurement.values]
            : measurement.values,
    })),
  });
  const completedAnalytics = trajectoryAnalytics(renderCampaignAt(multiMetricRecord, 0));
  const prefixAnalytics = trajectoryAnalytics(
    renderCampaignAt(
      {...multiMetricRecord, measurements: multiMetricRecord.measurements.slice(0, 1)},
      0,
    ),
  );

  expect(completedAnalytics).toEqual(prefixAnalytics);
  expect(completedAnalytics.axis).toContain('tok/s');

  const store = createCampaignHistoryStore(multiMetricRecord);
  expect(store.getState().campaign?.metricId).toBe('goodput');
  store.getState().campaign?.setMetric(latencyMetric.id);
  expect(store.getState().campaign?.metricId).toBe(latencyMetric.id);
  expect(store.getState().campaign?.pointIndex).toBe(lastIndex);
  store.close();
});
