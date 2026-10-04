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
  await expect(performance.getByText(/TARGET\s/)).toHaveCount(0);
  await expect(page.getByText('Benchmark v6', {exact: true})).toHaveCount(0);
  await expect(page.getByText('2,242.4 tok/s', {exact: true})).toBeVisible();
  await expect(
    page.getByText(/82 plotted points from Claude session a2d3319a-c2c4-444f-a440-4881f158f32c/),
  ).toBeVisible();
  await expect(
    page.getByText(/Round 15 continuation points from 9ae9a100-f067-4aa1-8334-2589bd573a6c/),
  ).toBeVisible();

  await page.getByRole('button', {name: 'Objective', exact: true}).click();
  await expect(
    page.getByRole('heading', {name: 'Maximize goodput under latency and correctness gates'}),
  ).toBeVisible();
  await expect(
    page.getByText(
      'Build a from-scratch OpenAI-compatible Qwen3.5-397B-A17B-MXFP4 server on one 4x MI300A node and maximize the corrected load-ramp goodput while preserving the campaign gates.',
      {exact: true},
    ),
  ).toBeVisible();
  await expect(page.getByRole('heading', {name: 'Metrics'})).toBeVisible();
  await expect(page.getByText('Goodput', {exact: true})).toBeVisible();
  for (const omittedObjectiveDetail of [
    '2,000 tok/s',
    'Reference latency',
    'Output quality',
    'Benchmark integrity',
    'Benchmark v5',
    'Benchmark v6',
    'Goodput ≥ 2,000 tok/s',
  ]) {
    await expect(page.getByText(omittedObjectiveDetail, {exact: false})).toHaveCount(0);
  }
});

test('follows the browser light and dark color scheme', async ({page}) => {
  await page.emulateMedia({colorScheme: 'light'});
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const pageColors = () =>
    page.evaluate(() => ({
      bodyBackground: getComputedStyle(document.body).backgroundColor,
      colorScheme: getComputedStyle(document.documentElement).colorScheme,
    }));

  await expect
    .poll(pageColors)
    .toEqual({bodyBackground: 'rgb(255, 255, 255)', colorScheme: 'light'});
  await page.emulateMedia({colorScheme: 'dark'});
  await expect.poll(pageColors).toEqual({bodyBackground: 'rgb(0, 0, 0)', colorScheme: 'dark'});
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
  const prefixBar = timeline.getByRole('button', {
    name: 'Prefix state reuse, accepted. Open workstream.',
  });
  await expect(prefixBar).toBeVisible();
  await expect(timeline.getByText('Prefix state reuse', {exact: true})).toHaveCount(0);
  for (const agentName of [
    'Orchestrator',
    'Implementer A',
    'Implementer B',
    'Profiler',
    'Evaluator',
    'Benchmark runner',
  ]) {
    await expect(timeline.getByText(agentName, {exact: true})).toHaveCount(0);
  }
  const packingCaption = await timeline.getByText(/workstreams packed into \d+ lanes/).innerText();
  const packedCounts = packingCaption.match(/(\d+) workstreams packed into (\d+) lanes/);
  expect(packedCounts).not.toBeNull();
  if (packedCounts === null) throw new Error('Timeline packing caption is missing its counts');
  expect(Number(packedCounts[2])).toBeLessThan(Number(packedCounts[1]));
  await prefixBar.click();
  const timelineWorkstream = page.getByRole('dialog', {name: 'Prefix state reuse'});
  await expect(timelineWorkstream).toBeVisible();
  await timelineWorkstream.getByRole('button', {name: 'Close workstream detail'}).click();

  await page
    .getByRole('region', {name: 'Workstream timeline'})
    .getByRole('button', {
      name: 'Agent activity',
    })
    .click();
  const agentTimeline = page.getByRole('region', {name: 'Agent activity timeline'});
  await expect(agentTimeline.getByRole('button', {name: /Implementer A/})).toBeVisible();
  await agentTimeline.getByRole('button', {name: 'Workstreams'}).click();
  await expect(page.getByRole('region', {name: 'Workstream timeline'})).toBeVisible();

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

test('sorts workstreams in a bounded table and opens row details', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const workstreams = page.getByRole('region', {name: 'Workstreams'});
  const slider = page.getByRole('slider', {name: 'Campaign measurement'});
  await slider.fill('110');
  await expect(slider).toHaveValue('110');
  const kanbanSort = workstreams.getByRole('combobox', {name: 'Sort workstreams'});
  await expect(kanbanSort).toBeVisible();
  await workstreams.getByRole('button', {name: 'Table'}).click();
  await expect(kanbanSort).toHaveCount(0);
  const table = workstreams.getByRole('table');
  await expect(table.getByRole('row')).toHaveCount(22);

  const firstRow = table.getByRole('row').nth(1);
  await expect(firstRow).toContainText('Serving engine integration');
  const startedHeader = table.locator('thead th').nth(2);
  await expect(startedHeader).toHaveAttribute('aria-sort', 'ascending');
  await startedHeader.getByRole('button', {name: 'Started'}).click();
  await expect(startedHeader).toHaveAttribute('aria-sort', 'descending');
  await expect(firstRow).toContainText('Early prefill launch');
  const endedHeader = table.locator('thead th').nth(3);
  await expect(endedHeader).toHaveAttribute('aria-sort', 'none');
  await endedHeader.getByRole('button', {name: 'Ended'}).click();
  await expect(endedHeader).toHaveAttribute('aria-sort', 'ascending');
  await expect(firstRow).toContainText('Prefix state reuse');
  await endedHeader.getByRole('button', {name: 'Ended'}).click();
  await expect(endedHeader).toHaveAttribute('aria-sort', 'descending');
  await expect(firstRow).toContainText('MTP speculative decoding');
  const elapsedHeader = table.locator('thead th').nth(4);
  await expect(elapsedHeader).toHaveAttribute('aria-sort', 'none');
  await elapsedHeader.getByRole('button', {name: 'Elapsed'}).click();
  await expect(elapsedHeader).toHaveAttribute('aria-sort', 'descending');
  await expect(firstRow).toContainText('Serving engine integration');
  await elapsedHeader.getByRole('button', {name: 'Elapsed'}).click();
  await expect(elapsedHeader).toHaveAttribute('aria-sort', 'ascending');
  await expect(firstRow).toContainText('Hot-expert dense paths');

  await expect(workstreams.getByText('Token usage not recorded', {exact: true})).toBeVisible();
  const tokensHeader = table.locator('thead th').nth(5);
  await expect(tokensHeader).toHaveAttribute('aria-sort', 'none');
  await expect(tokensHeader.getByRole('button', {name: 'Tokens'})).toBeDisabled();
  await expect(table.locator('tbody tr').first().getByTitle('Tokens not recorded')).toHaveText('—');

  const explorer = workstreams.locator('[aria-label="Workstream explorer"]');
  const bounds = await explorer.evaluate(element => {
    const style = getComputedStyle(element);
    return {
      clientHeight: element.clientHeight,
      maxHeight: style.maxHeight,
      overflowY: style.overflowY,
      scrollHeight: element.scrollHeight,
    };
  });
  expect(bounds.maxHeight).toBe('480px');
  expect(bounds.overflowY).toBe('auto');
  expect(bounds.scrollHeight).toBeGreaterThan(bounds.clientHeight);

  await table.getByRole('button', {name: 'Serving engine integration'}).click();
  const details = page.getByRole('dialog', {name: 'Serving engine integration'});
  await expect(details).toBeVisible();
  await details.getByRole('button', {name: 'Close workstream detail'}).click();

  await workstreams.getByRole('button', {name: 'Kanban'}).click();
  await expect(kanbanSort).toBeVisible();
  await workstreams.getByRole('button', {name: 'Table'}).click();
  await expect(kanbanSort).toHaveCount(0);
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
  await page.getByRole('button', {name: 'Objective', exact: true}).click();
  await expect(page.getByRole('heading', {name: 'Metrics'})).toBeVisible();
  await expect(page.getByText('2,000 tok/s', {exact: false})).toHaveCount(0);
  const viewport = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
});
