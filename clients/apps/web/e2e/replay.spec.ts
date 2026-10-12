import {expect, test} from '@playwright/test';

test('renders the replay-driven run viewer', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();
  // The fixture still carries the backend compatibility label `round-2`, but
  // the App heading is a run-state focus projection and must not render it.
  await expect(page.getByText('round-2', {exact: true})).toHaveCount(0);
  await expect(page.getByText('15 folded events')).toBeVisible();
  await expect(page.getByText('PASS', {exact: true})).toBeVisible();
  await page.screenshot({path: 'artifacts/web-replay.png', fullPage: true});
});

test('preserves multiline transcript text within the minimum viewport', async ({page}) => {
  const formattedContent = `one
  two
${'A'.repeat(512)}`;
  const baselineContent = `one
  two
short`;
  let content = baselineContent;
  await page.route(/\/__vibesys\/fixtures\/framework-events\.jsonl(?:\?.*)?$/, async route => {
    const response = await route.fetch();
    const events = (await response.text())
      .trimEnd()
      .split('\n')
      .map(line => JSON.parse(line) as Record<string, unknown>);
    let replacements = 0;
    const formatted = events.map(event => {
      if (event['sequence'] !== 15) return event;
      replacements += 1;
      return {
        ...event,
        type: 'agent_output_chunk',
        status: null,
        agent_kind: 'implementer',
        invocation_id: 'formatting-invocation',
        execution_id: 'formatting-execution',
        data: {kind: 'agent_output_chunk', channel: 'assistant', content},
      };
    });
    if (replacements !== 1) throw new Error(`Expected one replay event 15, got ${replacements}`);
    await route.fulfill({
      response,
      body: `${formatted.map(event => JSON.stringify(event)).join('\n')}\n`,
    });
  });
  await page.setViewportSize({width: 320, height: 640});
  await page.goto('/');
  await expect(page.locator('.transcript-content').filter({hasText: 'short'})).toHaveText(
    baselineContent,
  );
  const baselineDocumentWidth = await page.evaluate(() => document.documentElement.scrollWidth);

  content = formattedContent;
  await page.reload();

  const transcript = page.locator('.transcript-content').filter({hasText: 'A'.repeat(128)});
  await expect(transcript).toHaveText(formattedContent);
  const layout = await transcript.evaluate((element, payload) => {
    const text = element.querySelector('pre')?.firstChild;
    const entry = element.parentElement;
    const panel = element.closest('.panel');
    if (!(text instanceof Text) || entry === null || panel === null) {
      throw new Error('Transcript markup does not expose its text row and panel');
    }
    const character = (index: number): DOMRect => {
      const range = document.createRange();
      range.setStart(text, index);
      range.setEnd(text, index + 1);
      return range.getBoundingClientRect();
    };
    const first = character(payload.indexOf('one'));
    const second = character(payload.indexOf('two'));
    const style = getComputedStyle(element);
    return {
      documentWidth: document.documentElement.scrollWidth,
      entryClientWidth: entry.clientWidth,
      entryScrollWidth: entry.scrollWidth,
      firstLeft: first.left,
      firstTop: first.top,
      overflowWrap: style.overflowWrap,
      panelClientWidth: panel.clientWidth,
      panelScrollWidth: panel.scrollWidth,
      secondLeft: second.left,
      secondTop: second.top,
      whiteSpace: style.whiteSpace,
    };
  }, formattedContent);

  expect(layout.whiteSpace).toBe('pre-wrap');
  expect(layout.overflowWrap).toBe('anywhere');
  expect(layout.secondTop).toBeGreaterThan(layout.firstTop);
  expect(layout.secondLeft).toBeGreaterThan(layout.firstLeft);
  expect(layout.entryScrollWidth).toBeLessThanOrEqual(layout.entryClientWidth);
  expect(layout.panelScrollWidth).toBeLessThanOrEqual(layout.panelClientWidth);
  expect(layout.documentWidth).toBe(baselineDocumentWidth);
});

test('surfaces a malformed replay event and retries it', async ({page}) => {
  let corruptReplay = true;
  await page.route(/\/__vibesys\/fixtures\/framework-events\.jsonl(?:\?.*)?$/, async route => {
    if (corruptReplay) {
      await route.fulfill({
        contentType: 'application/x-ndjson',
        body:
          '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n' +
          '{"type":"agent_execution_started","sequence":2,"timestamp":"2026-09-27T00:00:01Z","data":{"kind":"agent_execution_started","stage":"implement"}}\n',
      });
    } else await route.continue();
  });

  await page.goto('/');
  // By test id, not by role: three banners share the alert role and the same
  // class, so the role alone cannot say which failure is on screen.
  await expect(page.getByTestId('replay-banner')).toContainText(
    'Replay fixture contains invalid event on line 2: Invalid server run event.data: activity must be present',
  );

  corruptReplay = false;
  await page.getByRole('button', {name: 'Retry'}).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();
});
