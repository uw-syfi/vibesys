import {expect, test} from '@playwright/test';

test('keeps the replay interactive after a cached history restoration', async ({page}) => {
  await page.goto('/');
  const slider = page.getByRole('slider', {name: 'Campaign measurement'});
  await expect(slider).toBeVisible();
  await slider.fill('2');
  await expect(page.getByText(/03\s*\/\s*111/)).toBeVisible();

  await page.evaluate(() => {
    window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
    window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
  });

  await slider.fill('12');
  await expect(page.getByText(/13\s*\/\s*111/)).toBeVisible();
});

test('keeps connection setup in one sheet with validation and focus return', async ({page}) => {
  await page.goto('/');
  const launcher = page.getByRole('button', {name: 'Connect', exact: true});
  await expect(page.getByRole('button', {name: 'Conversation'})).toHaveCount(0);
  await expect(page.locator('.run-state')).toHaveText('Preview · completed');
  await launcher.click();
  const dialog = page.getByRole('dialog', {name: 'Connect to a run'});
  await expect(dialog).toBeVisible();
  await dialog.getByRole('textbox', {name: 'Live gateway URL'}).fill('http://127.0.0.1:8765/');
  await dialog.getByRole('button', {name: 'Connect', exact: true}).click();
  await expect(dialog.getByRole('status')).toContainText(
    'The gateway URL must include its capability token',
  );
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(launcher).toBeFocused();
  await launcher.click();
  await expect(dialog.getByRole('textbox', {name: 'Live gateway URL'})).toHaveValue(
    'http://127.0.0.1:8765/',
  );
  await expect(dialog.getByRole('status')).toBeVisible();
});

test('keeps an open connection sheet and its draft when the campaign fixture finishes loading', async ({
  page,
}) => {
  let releaseFixture!: () => void;
  const fixtureGate = new Promise<void>(resolve => {
    releaseFixture = resolve;
  });
  await page.route(
    url => /\/trajectory-replay(?:-[\w-]+)?\.json$/.test(url.pathname),
    async route => {
      await fixtureGate;
      await route.continue();
    },
  );
  try {
    await page.goto('/');
    await page.getByRole('button', {name: 'Connect', exact: true}).click();
    const dialog = page.getByRole('dialog', {name: 'Connect to a run'});
    const input = dialog.getByRole('textbox', {name: 'Live gateway URL'});
    await input.fill('http://127.0.0.1:8765/');
    await expect(input).toBeFocused();

    releaseFixture();
    await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();
    await expect(dialog).toBeVisible();
    await expect(input).toHaveValue('http://127.0.0.1:8765/');
    await expect(input).toBeFocused();
  } finally {
    releaseFixture();
  }
});

test('keeps the connection action inside the campaign header at desktop and narrow widths', async ({
  page,
}) => {
  for (const width of [1280, 390]) {
    await page.setViewportSize({width, height: 768});
    await page.goto('/');
    const launcher = page.getByRole('button', {name: 'Connect', exact: true});
    const header = page.locator('.campaign-header');
    const brand = header.locator('.brand-lockup');
    const status = header.locator('.run-state');
    await expect(launcher).toBeVisible();
    await expect(status).toContainText('Preview');
    const trigger = await launcher.boundingBox();
    const bar = await header.boundingBox();
    const brandBox = await brand.boundingBox();
    const runState = await status.boundingBox();
    if (trigger === null || bar === null || brandBox === null || runState === null)
      throw new Error('Campaign header controls have no bounds');
    expect(brandBox.x + brandBox.width).toBeLessThanOrEqual(runState.x);
    expect(trigger.x).toBeGreaterThanOrEqual(bar.x);
    expect(trigger.x + trigger.width).toBeLessThanOrEqual(bar.x + bar.width);
    expect(trigger.y).toBeGreaterThanOrEqual(bar.y);
    expect(trigger.y + trigger.height).toBeLessThanOrEqual(bar.y + bar.height);
    expect(runState.x + runState.width).toBeLessThanOrEqual(trigger.x);
    expect(await header.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await launcher.click();
    const dialog = page.getByRole('dialog', {name: 'Connect to a run'});
    const sheet = await dialog.boundingBox();
    if (sheet === null) throw new Error('Connection sheet has no bounds');
    expect(sheet.width).toBeLessThanOrEqual(width);
    await expect(dialog.getByRole('textbox', {name: 'Live gateway URL'})).toBeVisible();
  }
});

test('keeps the campaign workspace and history position stable as the replay cursor advances', async ({
  page,
}) => {
  await page.setViewportSize({width: 1280, height: 768});
  await page.goto('/');
  const slider = page.getByRole('slider', {name: 'Campaign measurement'});
  await expect(slider).toBeVisible();
  const geometry = async () =>
    page.evaluate(() => {
      const rect = (selector: string): {top: number; height: number} => {
        const node = document.querySelector(selector);
        if (node === null) throw new Error(`Missing ${selector}`);
        const bounds = node.getBoundingClientRect();
        return {top: bounds.top, height: bounds.height};
      };
      return {
        chart: rect('.performance-panel'),
        currentWork: rect('.active-workstreams'),
        currentList: rect('.active-workstream-list'),
        timeline: rect('.timeline-track-viewport'),
        history: rect('.history-heading'),
      };
    });
  await slider.fill('0');
  const start = await geometry();
  for (const index of ['55', '110']) {
    await slider.fill(index);
    await expect(slider).toHaveValue(index);
    const frame = await geometry();
    for (const section of ['chart', 'currentWork', 'currentList', 'timeline', 'history'] as const) {
      expect(frame[section].top, `${section} top at cursor ${index}`).toBeCloseTo(
        start[section].top,
        0,
      );
      expect(frame[section].height, `${section} height at cursor ${index}`).toBeCloseTo(
        start[section].height,
        0,
      );
    }
  }
  const timeline = page.locator('.timeline-track-viewport');
  const lastLane = timeline.locator('.timeline-track-row').last();
  await lastLane.scrollIntoViewIfNeeded();
  await expect(lastLane).toBeInViewport();
  const lastWorkstream = lastLane.getByRole('button').first();
  await lastWorkstream.focus();
  await expect(lastWorkstream).toBeFocused();
  await lastWorkstream.press('Enter');
  await expect(page.getByRole('dialog')).toBeVisible();
});

test('shows a failed launch target inside the connection sheet', async ({page}) => {
  await page.goto('/?gateway=javascript:ignored');
  const dialog = page.getByRole('dialog', {name: 'Connect to a run'});
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('status')).toContainText('Use an http:// or https:// gateway URL');
  await dialog.getByRole('button', {name: 'Close gateway connection'}).click();
  await expect(page.getByRole('button', {name: 'Connect', exact: true})).toBeFocused();
});

test('keeps the plot, selected measurement, current work, and timeline in the desktop workspace', async ({
  page,
}) => {
  await page.setViewportSize({width: 1280, height: 800});
  await page.goto('/');
  const plot = page.getByRole('region', {name: 'Performance trajectory'});
  const measurement = page.getByRole('region', {name: 'Selected measurement'});
  const active = page.getByRole('region', {name: 'Active workstreams'});
  const timeline = page.getByRole('heading', {name: 'Workstream timeline'});
  await expect(plot).toBeVisible();
  await expect(measurement).toBeVisible();
  await expect(active).toBeVisible();
  const plotBox = await plot.boundingBox();
  const measurementBox = await measurement.boundingBox();
  const activeBox = await active.boundingBox();
  const timelineBox = await timeline.boundingBox();
  if (plotBox === null || measurementBox === null || activeBox === null || timelineBox === null) {
    throw new Error('Desktop workspace elements must have visible bounds');
  }
  expect(measurementBox.x).toBeGreaterThan(plotBox.x + plotBox.width);
  expect(activeBox.x + activeBox.width).toBeLessThanOrEqual(plotBox.x);
  expect(timelineBox.y).toBeLessThan(800);
  const objectiveTrigger = page.getByRole('button', {name: 'Objective details'});
  await expect(objectiveTrigger).toHaveCSS('font-size', '12px');
  await expect(objectiveTrigger).toHaveCSS('height', '32px');
  await page.getByRole('slider', {name: 'Campaign measurement'}).fill('2');
  await expect(active.getByRole('button', {name: /Serving engine integration/})).toBeVisible();
});

test('shows the compact brand and benchmark caveat without record provenance prose', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.locator('.brand-lockup')).toHaveText('VIBESYS');
  const mark = page.locator('.brand-lockup img');
  await expect(mark).toHaveAttribute('alt', '');
  expect(
    await mark.evaluate(
      image => image instanceof HTMLImageElement && image.complete && image.naturalWidth > 0,
    ),
  ).toBe(true);
  await expect(
    page
      .getByRole('region', {name: 'Performance trajectory'})
      .getByText('Benchmark changed mid-run'),
  ).toBeVisible();
  await expect(page.getByText(/82 plotted points recovered from campaign results/)).toHaveCount(0);
  await expect(page.locator('.campaign-footer')).toHaveCount(0);
  await page.getByRole('button', {name: 'Objective details'}).click();
  const notes = page.getByRole('dialog').locator('.objective-data-notes');
  await expect(notes.getByText(/82 plotted points recovered from campaign results/)).toBeHidden();
  await notes.locator('summary').click();
  await expect(notes.getByText(/agent attribution is unknown/)).toBeVisible();
});

test('renders the source-backed campaign as one continuous performance trajectory', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const performance = page.getByRole('region', {name: 'Performance trajectory'});
  await expect(performance).toContainText('111 recorded');
  await expect(
    performance.getByRole('group', {
      name: /Goodput measurements by campaign order\. Gray dots are individual runs; the step line is the running best\./,
    }),
  ).toHaveCount(1);
  await expect(performance.getByText(/TARGET\s/)).toHaveCount(0);
  await expect(page.getByText('Benchmark v6', {exact: true})).toHaveCount(0);
  await expect(page.getByText('2,242.4 tok/s', {exact: true})).toBeVisible();

  const replaySlider = page.getByRole('slider', {name: 'Campaign measurement'});
  await replaySlider.fill('110');
  await expect(replaySlider).toHaveValue('110');
  const bestLine = performance.locator('.performance-best-line');
  await expect(bestLine).toHaveCount(1);
  const fullPath = await bestLine.getAttribute('d');
  expect(fullPath).toMatch(/(?:^|\s)H\s/);
  expect(fullPath).toMatch(/(?:^|\s)V\s/);

  const pointStyles = await performance.locator('.chart-point-group').evaluateAll(groups =>
    groups.map(group => {
      const point = group.querySelector('.chart-point');
      const label = group.querySelector('button')?.getAttribute('aria-label') ?? '';
      return {
        fill: point === null ? '' : getComputedStyle(point).fill,
        label,
      };
    }),
  );
  expect(pointStyles.length).toBeGreaterThan(1);
  expect(new Set(pointStyles.map(point => point.fill))).toHaveProperty('size', 1);
  expect(pointStyles.every(point => point.fill.length > 0)).toBe(true);
  expect(pointStyles.some(point => point.label.includes(', accepted.'))).toBe(true);
  expect(pointStyles.some(point => point.label.includes(', rejected.'))).toBe(true);
  const neutralFill = await performance
    .locator('.chart-axis-label')
    .first()
    .evaluate(element => getComputedStyle(element).fill);
  expect(pointStyles[0]?.fill).toBe(neutralFill);

  await replaySlider.fill('12');
  await expect(replaySlider).toHaveValue('12');
  await expect(bestLine).not.toHaveAttribute('d', fullPath ?? '');
  const earlyPath = await bestLine.getAttribute('d');
  expect((earlyPath ?? '').length).toBeLessThan((fullPath ?? '').length);

  await expect(
    page.getByRole('heading', {name: 'Maximize goodput under latency and correctness gates'}),
  ).toBeVisible();
  await expect(
    page.getByText(
      'Build a from-scratch OpenAI-compatible Qwen3.5-397B-A17B-MXFP4 server on one 4x MI300A node and maximize the corrected load-ramp goodput while preserving the campaign gates.',
      {exact: true},
    ),
  ).toBeVisible();
  await expect(page.getByRole('button', {name: 'Objective', exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Dashboard', exact: true})).toHaveCount(0);
  const performanceTop = await performance.evaluate(element => element.getBoundingClientRect().top);
  const objectiveTrigger = page.getByRole('button', {name: 'Objective details'});
  await objectiveTrigger.click();
  const objectiveDialog = page.getByRole('dialog', {
    name: 'Maximize goodput under latency and correctness gates',
  });
  await expect(objectiveDialog).toBeVisible();
  const objectiveDetails = objectiveDialog.locator('.objective-details');
  await expect(objectiveDetails.getByRole('heading', {name: 'Metrics'})).toBeVisible();
  await expect(objectiveDetails.getByText('Goodput', {exact: true})).toBeVisible();
  await expect(objectiveDetails.getByText('Reference latency', {exact: true})).toBeVisible();
  await expect(objectiveDetails.getByText('Output quality', {exact: true})).toBeVisible();
  for (const omittedObjectiveDetail of [
    '2,000 tok/s',
    'Benchmark v5',
    'Benchmark v6',
    'Goodput ≥ 2,000 tok/s',
  ]) {
    await expect(page.getByText(omittedObjectiveDetail, {exact: false})).toHaveCount(0);
  }
  expect(
    await page
      .locator('.performance-panel')
      .evaluate(element => element.getBoundingClientRect().top),
  ).toBe(performanceTop);
  await page.keyboard.press('Escape');
  await expect(objectiveDialog).toHaveCount(0);
  await expect(objectiveTrigger).toBeFocused();
  await expect(performance).toBeVisible();
});

test('navigates plotted measurements with one chart tab stop and a visible keyboard focus', async ({
  page,
}) => {
  await page.goto('/');
  const slider = page.getByRole('slider', {name: 'Campaign measurement'});
  await slider.fill('110');
  const chart = page
    .getByRole('region', {name: 'Performance trajectory'})
    .getByRole('group', {name: /Goodput measurements by campaign order/});
  const rovingPoint = chart.locator('button[tabindex="0"]');
  await expect(rovingPoint).toHaveCount(1);
  await rovingPoint.focus();
  await expect(rovingPoint).toBeFocused();
  const focusedStroke = await chart
    .locator('.chart-point-group:focus-within .chart-point')
    .evaluate(element => getComputedStyle(element).stroke);
  const unfocusedStroke = await chart
    .locator('.chart-point-group:not(:focus-within) .chart-point')
    .first()
    .evaluate(element => getComputedStyle(element).stroke);
  expect(focusedStroke).not.toBe(unfocusedStroke);

  await page.keyboard.press('Home');
  await expect(slider).toHaveValue('0');
  await expect(chart.getByRole('button', {name: /Pre-fix load-ramp v5/})).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(slider).toHaveValue('1');
  await expect(chart.getByRole('button', {name: /prefix-fix 49e9d871/})).toBeFocused();
  await page.keyboard.press('End');
  await expect(slider).toHaveValue('110');
  await expect(chart.getByRole('button', {name: /MTP flag-off control, pair 2/})).toBeFocused();
  await expect(chart.locator('button[tabindex="0"]')).toHaveCount(1);
});

test('links the selected measurement to its workstream without moving replay on inspection', async ({
  page,
}) => {
  await page.goto('/');
  const slider = page.getByRole('slider', {name: 'Campaign measurement'});
  await slider.fill('2');
  const timeline = page.getByRole('region', {name: 'Workstream timeline'});
  const history = page.getByRole('region', {name: 'Work history'});
  const selectedBar = timeline.getByRole('button', {
    name: /Prefix state reuse.*contains selected measurement/,
  });
  const selectedRow = history.getByRole('button', {
    name: /Prefix state reuse.*contains selected measurement/,
  });
  await expect(selectedBar).toHaveAttribute('aria-current', 'true');
  await expect(selectedRow).toHaveAttribute('aria-current', 'true');
  await selectedRow.click();
  await expect(page.getByRole('dialog', {name: 'Prefix state reuse'})).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(slider).toHaveValue('2');
  await expect(selectedBar).toHaveAttribute('aria-current', 'true');
  await slider.fill('0');
  await expect(
    page.getByRole('region', {name: 'Active workstreams'}).getByRole('button', {
      name: /Serving engine integration.*contains selected measurement/,
    }),
  ).toHaveAttribute('aria-current', 'true');
});

test('follows the browser color scheme across the full desktop and narrow viewport', async ({
  page,
}) => {
  const pageColors = () =>
    page.locator('.campaign-shell').evaluate(element => {
      const bounds = element.getBoundingClientRect();
      return {
        background: getComputedStyle(element).backgroundColor,
        colorScheme: getComputedStyle(element).colorScheme,
        coversViewport:
          bounds.left === 0 &&
          bounds.right === document.documentElement.clientWidth &&
          bounds.height >= window.innerHeight,
      };
    });

  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height: 1000});
    await page.emulateMedia({colorScheme: 'light'});
    await page.goto('/');
    await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();
    await expect.poll(pageColors).toEqual({
      background: 'rgb(255, 255, 255)',
      colorScheme: 'light',
      coversViewport: true,
    });
    await page.emulateMedia({colorScheme: 'dark'});
    await expect.poll(pageColors).toEqual({
      background: 'rgb(0, 0, 0)',
      colorScheme: 'dark',
      coversViewport: true,
    });
  }
});

test('replay slider reveals workstreams over time and preserves the final best result', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const slider = page.getByRole('slider', {name: 'Campaign measurement'});
  await expect(slider).toHaveAttribute('max', '110');
  await slider.fill('2');

  const workstreams = page.getByRole('region', {name: 'Work history'});
  await expect(workstreams.getByText('Prefix state reuse', {exact: true})).toBeVisible();
  await expect(
    workstreams.getByText('Decode graphs and overlap scheduling', {exact: true}),
  ).toHaveCount(0);
  await expect(page.getByText(/03\s*\/\s*111/)).toBeVisible();

  await slider.fill('108');
  await expect(page.getByText('MTP k=2 speculative decoding, pair 1', {exact: true})).toBeVisible();
  await expect(page.getByText('2,242.4 tok/s', {exact: true})).toBeVisible();
  await expect(page.getByText('Trigger agent not recorded', {exact: true})).toHaveCount(0);
  await expect(page.getByText('Runner not recorded', {exact: true})).toHaveCount(0);

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
  await expect(prefixBar).toContainText('Prefix state reuse');
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
  await expect(timeline.getByText(/workstreams packed into \d+ lanes/)).toHaveCount(0);
  await expect(timeline.getByRole('slider', {name: 'Timeline playhead'})).toHaveCount(1);
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
  await expect(
    agentTimeline.getByRole('button', {name: 'Implementer A', exact: true}),
  ).toBeVisible();
  await agentTimeline.getByRole('button', {name: 'Workstreams'}).click();
  await expect(page.getByRole('region', {name: 'Workstream timeline'})).toBeVisible();

  const workstreams = page.getByRole('region', {name: 'Work history'});
  await workstreams
    .getByRole('searchbox', {name: 'Search completed workstreams'})
    .fill('MTP speculative decoding');
  const launchWorkstream = workstreams.getByRole('button', {
    name: /MTP speculative decoding.*Open workstream/,
  });
  await launchWorkstream.click();
  const workstreamDialog = page.getByRole('dialog', {name: 'MTP speculative decoding'});
  await expect(workstreamDialog).toContainText(
    'Serve MTP draft and captured verification rounds while preserving exact output.',
  );
  await expect(workstreamDialog).toContainText('6 observations');
  expect(await workstreamDialog.evaluate(element => element.contains(document.activeElement))).toBe(
    true,
  );
  await page.keyboard.press('Escape');
  await expect(workstreamDialog).toHaveCount(0);
  await expect(launchWorkstream).toBeFocused();
  await launchWorkstream.click();
  await workstreamDialog.getByRole('button', {name: /Implementer A.*1 turn\b/}).click();

  const agentDialog = page.getByRole('dialog', {name: 'Implementer A'});
  const turns = agentDialog.getByRole('listitem');
  await expect(turns).toHaveCount(2);
  await expect(turns.nth(0)).toContainText('TURN 01');
  await expect(turns.nth(0)).toContainText('Turn-suffix folding');
  await expect(turns.nth(0)).toContainText('1053.3905 to 1154.4770 tok/s');
  await expect(turns.nth(1)).toContainText('TURN 02');
  await expect(turns.nth(1)).toContainText('MTP speculative decoding');
  await expect(turns.nth(1)).toContainText('1905.4 to 2242.4 tok/s');
  await agentDialog.getByRole('button', {name: 'Back to workstream'}).click();
  await expect(workstreamDialog).toBeVisible();
  await workstreamDialog.getByRole('button', {name: 'Close workstream detail'}).click();
  await expect(launchWorkstream).toBeFocused();
});

test('searches and filters a fixed-height completed history without losing details', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();

  const workstreams = page.getByRole('region', {name: 'Work history'});
  const results = workstreams.getByRole('rowgroup', {name: 'Completed history results'});
  const search = workstreams.getByRole('searchbox', {name: 'Search completed workstreams'});
  const filter = workstreams.getByRole('combobox', {name: 'Filter history by outcome'});
  await expect(workstreams.getByText('21 of 21 workstreams')).toBeVisible();
  const height = await results.evaluate(element => element.clientHeight);
  expect(height).toBe(312);
  expect(await results.getByRole('row').count()).toBeLessThan(21);
  await expect(workstreams.getByText('Token usage not recorded')).toHaveCount(0);
  await expect(workstreams.locator('.history-columns')).not.toContainText('Tokens');

  await search.fill('Turn-suffix folding');
  await expect(workstreams.getByText('1 of 21 workstreams')).toBeVisible();
  await expect(results.getByRole('row')).toHaveCount(1);
  await results.getByRole('button', {name: /Turn-suffix folding.*Open workstream/}).click();
  const details = page.getByRole('dialog', {name: 'Turn-suffix folding'});
  await expect(details).toBeVisible();
  await details.getByRole('button', {name: 'Close workstream detail'}).click();

  await search.fill('');
  await filter.selectOption('rejected');
  await expect(workstreams.getByText('3 of 21 workstreams')).toBeVisible();
  await workstreams.getByRole('button', {name: 'Sort by Started', exact: true}).click();
  await expect(results.getByRole('row').first()).toContainText('Prefill scheduling');
  await search.fill('no such workstream');
  await expect(results.getByText('No completed workstreams match this view.')).toBeVisible();
  expect(await results.evaluate(element => element.clientHeight)).toBe(height);
  await expect(workstreams.getByRole('button', {name: 'Next'})).toHaveCount(0);
  await expect(workstreams.getByRole('button', {name: 'Kanban'})).toHaveCount(0);
  await expect(workstreams.getByRole('button', {name: 'Table'})).toHaveCount(0);
});

test('keeps active work visible and anchors completed rows as the campaign advances', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();
  const slider = page.getByRole('slider', {name: 'Campaign measurement'});
  const workstreams = page.getByRole('region', {name: 'Work history'});
  const active = page.getByRole('region', {name: 'Active workstreams'});
  const results = workstreams.getByRole('rowgroup', {name: 'Completed history results'});
  await slider.fill('2');
  await expect(active.getByRole('button', {name: /Serving engine integration/})).toBeVisible();
  await expect(workstreams.getByText('1 of 1 workstreams')).toBeVisible();
  await results.getByRole('button', {name: /Prefix state reuse/}).focus();
  await results.getByRole('button', {name: /Prefix state reuse/}).press('ArrowDown');
  await page.keyboard.press('Enter');
  const details = page.getByRole('dialog', {name: 'Prefix state reuse'});
  await expect(details).toBeVisible();
  await details.getByRole('button', {name: 'Close workstream detail'}).click();

  await slider.fill('70');
  await workstreams.getByRole('button', {name: 'Sort by Started', exact: true}).click();
  await results.scrollIntoViewIfNeeded();
  const topRowId = () =>
    results.evaluate(element => {
      const bounds = element.getBoundingClientRect();
      const target = document.elementFromPoint(bounds.left + 25, bounds.top + 20);
      return (
        target?.closest('button[data-workstream-id]')?.getAttribute('data-workstream-id') ?? null
      );
    });
  const anchoredId = await slider.evaluate(replayInput => {
    const viewport = document.querySelector<HTMLElement>(
      '[aria-label="Completed history results"]',
    );
    if (viewport === null) return null;
    viewport.scrollTop = 104;
    const bounds = viewport.getBoundingClientRect();
    const anchored = document
      .elementFromPoint(bounds.left + 25, bounds.top + 20)
      ?.closest('button[data-workstream-id]')
      ?.getAttribute('data-workstream-id');
    // Dispatch the replay change before the queued scroll event can update React state.
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(
      replayInput,
      '110',
    );
    replayInput.dispatchEvent(new Event('input', {bubbles: true}));
    return anchored ?? null;
  });
  expect(anchoredId).not.toBeNull();
  await expect(slider).toHaveValue('110');
  await expect.poll(topRowId).toBe(anchoredId);
  await expect(workstreams.getByText('21 of 21 workstreams')).toBeVisible();
  expect(await results.getByRole('row').count()).toBeLessThan(21);
});

test('surfaces a campaign load failure and retries it', async ({page}) => {
  let failRequest = true;
  await page.route(
    url => /\/trajectory-replay(?:-[\w-]+)?\.json$/.test(url.pathname),
    async route => {
      if (failRequest) await route.fulfill({status: 503, body: 'temporarily unavailable'});
      else await route.continue();
    },
  );

  await page.goto('/');
  await expect(page.getByTestId('replay-banner')).toContainText(
    'Campaign fixture request failed with 503',
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
  await page.getByRole('button', {name: 'Objective details'}).click();
  await expect(page.getByRole('heading', {name: 'Metrics'})).toBeVisible();
  await expect(page.getByText('2,000 tok/s', {exact: false})).toHaveCount(0);
  const viewport = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
});

test('surfaces a malformed campaign record and retries it', async ({page}) => {
  let corruptCampaign = true;
  await page.route(
    url => /\/trajectory-replay(?:-[\w-]+)?\.json$/.test(url.pathname),
    async route => {
      if (corruptCampaign) {
        await route.fulfill({contentType: 'application/json', body: '{"schemaVersion":99}'});
      } else await route.continue();
    },
  );

  await page.goto('/');
  // By test id, not by role: three banners share the alert role and the same
  // class, so the role alone cannot say which failure is on screen.
  await expect(page.getByTestId('replay-banner')).toContainText(
    'Invalid replay scenario at $.schemaVersion: expected 1',
  );

  corruptCampaign = false;
  await page.getByRole('button', {name: 'Retry'}).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.getByRole('heading', {name: 'Qwen3.5-397B-A17B on 4x MI300A'})).toBeVisible();
});
