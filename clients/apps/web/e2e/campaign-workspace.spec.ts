import {expect, test} from '@playwright/test';

test('gives active work its own readable desktop column without scrolling measurement details', async ({
  page,
}) => {
  for (const width of [1440, 1280]) {
    await page.setViewportSize({width, height: 1000});
    await page.goto('/');
    await page.getByRole('slider', {name: 'Campaign measurement'}).fill('55');
    const active = page.getByRole('region', {name: 'Active workstreams'});
    const plot = page.getByRole('region', {name: 'Performance trajectory'});
    const measurement = page.getByRole('region', {name: 'Selected measurement'});
    const [activeBox, plotBox, measurementBox] = await Promise.all([
      active.boundingBox(),
      plot.boundingBox(),
      measurement.boundingBox(),
    ]);
    if (activeBox === null || plotBox === null || measurementBox === null)
      throw new Error('Workspace regions must be visible');
    expect(activeBox.x + activeBox.width).toBeLessThanOrEqual(plotBox.x);
    expect(plotBox.x + plotBox.width).toBeLessThanOrEqual(measurementBox.x);
    expect(activeBox.y).toBeCloseTo(plotBox.y, 0);
    expect(measurementBox.y).toBeCloseTo(plotBox.y, 0);
    const rows = active.getByRole('button');
    expect(await rows.count()).toBeGreaterThanOrEqual(4);
    for (const row of (await rows.all()).slice(0, 4)) await expect(row).toBeInViewport({ratio: 1});
    expect(
      await measurement.evaluate(element => element.scrollHeight <= element.clientHeight),
    ).toBe(true);
    await expect(measurement.getByText('Trigger agent not recorded')).toHaveCount(0);
    await expect(measurement.getByText('Runner not recorded')).toHaveCount(0);
  }
});

test('fits the timeline without horizontal scrolling and preserves replay while zooming', async ({
  page,
}) => {
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto('/');
  const cursor = page.getByRole('slider', {name: 'Campaign measurement'});
  await cursor.fill('55');
  const timeline = page.getByRole('region', {name: 'Workstream timeline'});
  const playhead = timeline.getByRole('slider', {name: 'Timeline playhead'});
  await playhead.focus();
  await playhead.press('ArrowRight');
  await expect(cursor).toHaveValue('56');
  await playhead.press('ArrowLeft');
  await expect(cursor).toHaveValue('55');
  await expect(timeline.getByRole('button', {name: 'Fit timeline'})).toBeVisible();
  expect(await timeline.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await expect(timeline.getByRole('slider', {name: 'Pan timeline'})).toHaveCount(0);
  await timeline.getByRole('button', {name: 'Zoom in timeline'}).click();
  const pan = timeline.getByRole('slider', {name: 'Pan timeline'});
  await expect(pan).toBeVisible();
  await pan.focus();
  await pan.press('End');
  await expect(cursor).toHaveValue('55');
  await playhead.focus();
  await playhead.press('ArrowRight');
  await expect(cursor).toHaveValue('56');
  await playhead.press('ArrowLeft');
  await expect(cursor).toHaveValue('55');
  await timeline.getByRole('button', {name: 'Fit timeline'}).click();
  await expect(pan).toHaveCount(0);
  await expect(cursor).toHaveValue('55');
  const selected = timeline.getByRole('button', {name: /contains selected measurement/});
  await selected.scrollIntoViewIfNeeded();
  await selected.focus();
  await selected.press('Enter');
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(selected).toBeFocused();
  await expect(cursor).toHaveValue('55');
});

test('describes agent intervals as activity rather than workstream outcomes', async ({page}) => {
  await page.goto('/');
  await page.getByRole('slider', {name: 'Campaign measurement'}).fill('55');
  await page.getByRole('button', {name: 'Agent activity', exact: true}).click();
  const timeline = page.getByRole('region', {name: 'Agent activity timeline'});
  await expect(timeline.getByText('benchmark_runner', {exact: true})).toHaveCount(0);
  await expect(timeline.getByRole('button', {name: /, accepted/})).toHaveCount(0);
  await expect(timeline.getByRole('button', {name: /, rejected/})).toHaveCount(0);
  await expect(timeline.getByRole('button', {name: 'Benchmark runner', exact: true})).toBeVisible();
});

test('sorts history from column headers with announced direction and keyboard activation', async ({
  page,
}) => {
  await page.goto('/');
  const history = page.getByRole('region', {name: 'Work history'});
  await expect(history.getByRole('combobox', {name: 'Sort completed workstreams'})).toHaveCount(0);
  const results = history.getByRole('rowgroup', {name: 'Completed history results'});
  for (const field of ['Workstream', 'Outcome', 'Elapsed']) {
    const button = history.getByRole('button', {name: `Sort by ${field}`, exact: true});
    const heading = history.getByRole('columnheader', {name: `Sort by ${field}`, exact: true});
    await button.focus();
    await button.press('Enter');
    await expect(heading).toHaveAttribute('aria-sort', 'ascending');
    const firstAscending = await results.getByRole('row').first().innerText();
    await button.press('Enter');
    await expect(heading).toHaveAttribute('aria-sort', 'descending');
    const firstDescending = await results.getByRole('row').first().innerText();
    expect(firstDescending).not.toBe(firstAscending);
    await expect(history.locator('[aria-sort="ascending"]')).toHaveCount(0);
    await expect(history.locator('[aria-sort="descending"]')).toHaveCount(1);
  }
  const nameSort = history.getByRole('button', {name: 'Sort by Workstream', exact: true});
  await nameSort.click();
  const names = await results
    .getByRole('row')
    .evaluateAll(rows => rows.map(row => row.querySelector('td')?.textContent?.trim() ?? ''));
  expect(names.length).toBeGreaterThan(1);
  expect(names.every(name => name.length > 0)).toBe(true);
  expect(names).toEqual([...names].sort((left, right) => left.localeCompare(right)));
});

test('keeps fit timeline and measurement content usable on a narrow screen', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await page.goto('/');
  await page.getByRole('slider', {name: 'Campaign measurement'}).fill('55');
  const measurement = page.getByRole('region', {name: 'Selected measurement'});
  expect(await measurement.evaluate(element => element.scrollHeight <= element.clientHeight)).toBe(
    true,
  );
  const timeline = page.getByRole('region', {name: 'Workstream timeline'});
  await timeline.getByRole('button', {name: 'Zoom in timeline'}).click();
  await expect(timeline.getByRole('slider', {name: 'Pan timeline'})).toBeVisible();
  await timeline.getByRole('button', {name: 'Fit timeline'}).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    ),
  ).toBe(true);
});

test('shows zero-duration workstreams as settled at their exact finish time', async ({page}) => {
  await page.goto('/');
  const cursor = page.getByRole('slider', {name: 'Campaign measurement'});
  const timeline = page.getByRole('region', {name: 'Workstream timeline'});
  for (const {index, title, outcome} of [
    {index: '11', title: 'Hot-expert dense paths', outcome: 'rejected'},
    {index: '26', title: 'KV cache and lane capacity', outcome: 'accepted'},
    {index: '36', title: 'Mixed-step graphs', outcome: 'accepted'},
  ]) {
    await cursor.fill(index);
    const clip = timeline.getByRole('button', {name: new RegExp(`${title}, ${outcome}`)});
    await clip.scrollIntoViewIfNeeded();
    await expect(clip).toBeInViewport();
    await clip.focus();
    await clip.press('Enter');
    await expect(page.getByRole('dialog', {name: title})).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(cursor).toHaveValue(index);
  }
});
