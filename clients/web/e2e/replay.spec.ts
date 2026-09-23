import {expect, test} from '@playwright/test';

test('renders the replay-driven run viewer', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'round-2'})).toBeVisible();
  await expect(page.getByText('15 folded events')).toBeVisible();
  await expect(page.getByText('PASS', {exact: true})).toBeVisible();
  await page.screenshot({path: 'artifacts/web-replay.png', fullPage: true});
});
