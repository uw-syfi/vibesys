import {expect, test} from '@playwright/test';

test('renders the replay-driven run viewer', async ({page}) => {
  await page.goto('/');
  await expect(page.getByText('decode-throughput.md')).toBeVisible();
  await expect(page.getByText('Completed', {exact: true})).toBeVisible();
  await expect(page.getByLabel('Replay. Live gateway URL')).toBeVisible();
  await page.screenshot({path: 'artifacts/web-replay.png', fullPage: true});
});
