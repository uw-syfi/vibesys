import {expect, test} from '@playwright/test';

test('renders the campaign objective and its metric contract', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();
  await page.getByRole('button', {name: 'Objective & gates'}).click();

  await expect(
    page.getByRole('heading', {
      name: 'Improve serving goodput under the campaign latency and quality constraints',
    }),
  ).toBeVisible();
  await expect(page.getByText('Total token throughput', {exact: true})).toBeVisible();
  await expect(page.getByText('p95 TTFT, turn 2+', {exact: true})).toBeVisible();
  await expect(page.getByText('Peak goodput', {exact: true})).toBeVisible();
  await expect(page.getByText('p95 TTFT at reference', {exact: true})).toBeVisible();
  await expect(page.getByText(/Benchmark v4.*Benchmark v5 at event 12/)).toBeVisible();
  await page.screenshot({path: 'artifacts/web-replay.png', fullPage: true});
});

test('replays active and completed workstreams and opens agent turns from a measurement', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();
  await page.screenshot({path: 'artifacts/web-dashboard.png', fullPage: true});

  await page.getByRole('slider', {name: 'Replay measurement'}).fill('3');
  const workstreams = page.getByRole('region', {name: 'Workstreams'});
  await expect(workstreams.getByRole('button', {name: /Prefix cache/})).toContainText('Accepted');
  await expect(workstreams.getByRole('button', {name: /One-shot all-reduce/})).toContainText(
    'Active',
  );

  await page.getByRole('button', {name: /Prefix cache, 55\.6 tok\/s, accepted/}).click();
  await expect(page.getByText('Triggered by Dynamic implementer A', {exact: true})).toBeVisible();
  await expect(page.getByText('Run by Framework benchmark runner', {exact: true})).toBeVisible();
  await page.getByRole('button', {name: 'Inspect workstream'}).click();

  const workstreamDialog = page.getByRole('dialog', {name: 'Prefix cache'});
  await expect(workstreamDialog.getByText('in progress', {exact: true})).toBeVisible();
  await expect(workstreamDialog.getByText('55.6 tok/s')).toBeVisible();
  await workstreamDialog.getByRole('button', {name: /Dynamic implementer A/}).click();

  const agentDialog = page.getByRole('dialog', {name: 'Dynamic implementer A'});
  const turns = agentDialog.getByRole('listitem');
  await expect(turns).toHaveCount(1);
  const firstTurn = turns.nth(0);
  await expect(firstTurn).toContainText('TURN 01');
  await expect(firstTurn).toContainText('Tool call RunBenchmark');
  await expect(firstTurn).toContainText('Tool result RunBenchmark');
  await expect(firstTurn).toContainText('Candidate recorded as accepted.');
  const orderedMessages = (await firstTurn.locator('article').allInnerTexts()).map(text =>
    text.replace(/\s+/g, ' '),
  );
  expect(orderedMessages).toEqual([
    expect.stringContaining('The prefix-cache landmark'),
    expect.stringContaining('TOOL CALL RUNBENCHMARK'),
    expect.stringContaining('TOOL RESULT RUNBENCHMARK'),
    expect.stringContaining('Candidate recorded as accepted.'),
  ]);

  await agentDialog.getByRole('button', {name: 'Close agent trajectory'}).click();
  const slider = page.getByRole('slider', {name: 'Replay measurement'});
  const finalMeasurementIndex = await slider.getAttribute('max');
  expect(finalMeasurementIndex).not.toBeNull();
  await slider.fill(finalMeasurementIndex ?? '0');
  await expect(workstreams.getByRole('button', {name: /C112 concurrency expansion/})).toContainText(
    'Rejected',
  );
  await page.getByRole('combobox', {name: 'Performance metric'}).selectOption('v5-peak-goodput');
  await page
    .getByRole('button', {name: /MTP speculative decoding, 2,242\.4 tok\/s, accepted/})
    .click();
  await expect(page.getByText('Triggered by Dynamic implementer A', {exact: true})).toBeVisible();
  await page.getByRole('button', {name: 'Inspect workstream'}).click();
  const mtpDialog = page.getByRole('dialog', {name: 'MTP speculative decoding'});
  await mtpDialog.getByRole('button', {name: /Dynamic implementer A/}).click();
  const finalTrajectory = page.getByRole('dialog', {name: 'Dynamic implementer A'});
  await expect(finalTrajectory.getByRole('listitem')).toHaveCount(2);
  await expect(finalTrajectory.getByRole('listitem').nth(1)).toContainText('TURN 02');
  await page.screenshot({path: 'artifacts/web-agent-trajectory.png', fullPage: true});
});

test('surfaces a replay load failure and retries it', async ({page}) => {
  let failRequest = true;
  await page.route(/\/__vibesys\/fixtures\/framework-events\.jsonl(?:\?.*)?$/, async route => {
    if (failRequest) await route.fulfill({status: 503, body: 'temporarily unavailable'});
    else await route.continue();
  });

  await page.goto('/');
  // By test id, not by role: three banners share the alert role and the same
  // class, so the role alone cannot say which failure is on screen.
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
  await expect(page.getByText('Peak goodput', {exact: true})).toBeVisible();
  const viewport = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
  await page.screenshot({path: 'artifacts/web-mobile.png', fullPage: true});
});
