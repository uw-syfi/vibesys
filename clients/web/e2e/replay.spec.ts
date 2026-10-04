import {expect, test} from '@playwright/test';

test('renders the source-backed campaign as one continuous performance trajectory', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const performance = page.getByRole('region', {name: 'Performance trajectory'});
  await expect(performance).toContainText('111 recorded');
  await expect(
    performance.getByRole('img', {
      name: 'Goodput measurements by campaign order. Select a point for details.',
    }),
  ).toHaveCount(1);
  await expect(page.getByText('Benchmark v6', {exact: true})).toHaveCount(0);
  await expect(page.getByText('2,242.4 tok/s', {exact: true})).toBeVisible();
  await expect(
    page.getByText(/82 plotted points from Claude session a2d3319a-c2c4-444f-a440-4881f158f32c/),
  ).toBeVisible();
  await expect(
    page.getByText(/Round 15 continuation points from 9ae9a100-f067-4aa1-8334-2589bd573a6c/),
  ).toBeVisible();

  await page.getByRole('button', {name: 'Objective & gates'}).click();
  await expect(
    page.getByRole('heading', {name: 'Maximize goodput under latency and correctness gates'}),
  ).toBeVisible();
  await expect(page.getByText('Goodput ≥ 2,000 tok/s', {exact: true})).toBeVisible();
  await expect(page.getByText(/Benchmark v5.*Benchmark v6 at event 74/)).toBeVisible();
  await expect(page.getByText(/one continuous goodput scale/)).toBeVisible();
});

test('replay slider reveals workstreams over time and preserves the final best result', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const slider = page.getByRole('slider', {name: 'Campaign measurement'});
  await expect(slider).toHaveAttribute('max', '110');
  await slider.fill('2');

  const workstreams = page.getByRole('region', {name: 'Workstreams'});
  await expect(workstreams.getByText('Prefix state reuse', {exact: true})).toBeVisible();
  await expect(
    workstreams.getByText('Decode graphs and overlap scheduling', {exact: true}),
  ).toHaveCount(0);
  await expect(page.getByText(/03\s*\/\s*111/)).toBeVisible();

  await slider.fill('108');
  await expect(page.getByText('MTP k=2 speculative decoding, pair 1', {exact: true})).toBeVisible();
  await expect(page.getByText('2,242.4 tok/s', {exact: true})).toBeVisible();
  await expect(page.getByText('Trigger agent not recorded', {exact: true})).toBeVisible();
  await expect(page.getByText('Runner not recorded', {exact: true})).toBeVisible();

  await slider.fill('110');
  await expect(page.getByText('MTP flag-off control, pair 2', {exact: true})).toBeVisible();
  await expect(page.getByText('1,898.3 tok/s', {exact: false})).toBeVisible();
  await expect(page.getByText('2,242.4 tok/s', {exact: true})).toBeVisible();
  await expect(page.getByText(/111\s*\/\s*111/)).toBeVisible();
});

test('presents hypothesis workstreams with inspectable agent trajectories', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const timeline = page.getByRole('region', {name: 'Workstream timeline'});
  await expect(timeline.getByText('Serving engine integration', {exact: true})).toBeVisible();
  await expect(timeline.getByText('Turn-suffix folding', {exact: true})).toBeVisible();
  await expect(timeline.getByText('Implementer A', {exact: true})).toHaveCount(0);
  await expect(timeline.getByText('Profiler', {exact: true})).toHaveCount(0);

  const workstreams = page.getByRole('region', {name: 'Workstreams'});
  await expect(workstreams.getByText('MTP speculative decoding', {exact: true})).toBeVisible();
  await expect(workstreams.getByText('Sequence-parallel collectives', {exact: true})).toBeVisible();
  await expect(workstreams.getByText('Turn-suffix folding', {exact: true})).toBeVisible();
  await expect(workstreams.getByText('Profiling', {exact: true})).toHaveCount(0);
  await expect(workstreams.getByText('Benchmarking', {exact: true})).toHaveCount(0);

  await workstreams.getByRole('button', {name: /MTP speculative decoding/}).click();
  const workstreamDialog = page.getByRole('dialog', {name: 'MTP speculative decoding'});
  await expect(workstreamDialog).toContainText(
    'Serve MTP draft and captured verification rounds while preserving exact output.',
  );
  await expect(workstreamDialog).toContainText('6 observations');
  await workstreamDialog.getByRole('button', {name: /Implementer A.*1 turns/}).click();

  const agentDialog = page.getByRole('dialog', {name: 'Implementer A'});
  const turns = agentDialog.getByRole('listitem');
  await expect(turns).toHaveCount(2);
  await expect(turns.nth(0)).toContainText('TURN 01');
  await expect(turns.nth(0)).toContainText('Turn-suffix folding');
  await expect(turns.nth(0)).toContainText('1053.3905 to 1154.4770 tok/s');
  await expect(turns.nth(1)).toContainText('TURN 02');
  await expect(turns.nth(1)).toContainText('MTP speculative decoding');
  await expect(turns.nth(1)).toContainText('1905.4 to 2242.4 tok/s');
});

test('surfaces a replay load failure and retries it', async ({page}) => {
  let failRequest = true;
  await page.route(/\/__vibesys\/fixtures\/framework-events\.jsonl(?:\?.*)?$/, async route => {
    if (failRequest) await route.fulfill({status: 503, body: 'temporarily unavailable'});
    else await route.continue();
  });

  await page.goto('/');
  await expect(page.getByTestId('replay-banner')).toContainText(
    'Replay fixture request failed with 503',
  );

  failRequest = false;
  await page.getByRole('button', {name: 'Retry'}).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();
});

test('keeps the replay navigable on a narrow viewport', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();
  await page.getByRole('button', {name: 'Objective & gates'}).click();
  await expect(page.getByText('Goodput ≥ 2,000 tok/s', {exact: true})).toBeVisible();
  const viewport = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
});
