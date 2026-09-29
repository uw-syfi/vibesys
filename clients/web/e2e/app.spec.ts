import {expect, test} from '@playwright/test';
import {mockGateway} from './gateway.js';

test('the page follows the mocked demo gateway', async ({page}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  await expect(page.getByText('llm-serve').first()).toBeVisible();
  await expect
    .poll(() => gateway.requests.some(request => request.type === 'query.experiments'))
    .toBe(true);
});
