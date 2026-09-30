import {expect, test} from '@playwright/test';

test('renders the replay-driven run viewer', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'round-2'})).toBeVisible();
  await expect(page.getByText('15 folded events')).toBeVisible();
  await expect(page.getByText('PASS', {exact: true})).toBeVisible();
  await page.screenshot({path: 'artifacts/web-replay.png', fullPage: true});
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
  await expect(page.getByRole('heading', {name: 'round-2'})).toBeVisible();
});
