import {expect, test} from '@playwright/test';

test('renders the replay-driven run viewer', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();
  // The fixture still carries the backend compatibility label `round-2`, but
  // the App heading is a core-state focus projection and must not render it.
  await expect(page.getByText('round-2', {exact: true})).toHaveCount(0);
  await expect(page.getByText('15 folded events')).toBeVisible();
  await expect(page.getByText('PASS', {exact: true})).toBeVisible();
  await page.screenshot({path: 'artifacts/web-replay.png', fullPage: true});
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
