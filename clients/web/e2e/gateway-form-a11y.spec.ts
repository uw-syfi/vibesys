import {expect, type Locator, test} from '@playwright/test';

const FIRST_CAPABILITY = 'http://127.0.0.1:8765/?token=first';
const SECOND_CAPABILITY = 'http://127.0.0.1:8765/?token=second';
const THIRD_CAPABILITY = 'http://127.0.0.1:8765/?token=third';
const CANCELLED = 'Gateway connection cancelled. Retry when ready.';
const FAILURE = 'Gateway rejected the capability (HTTP 403)';

interface HarnessWindow extends Window {
  gatewayHarness: {
    readonly rejectCurrent: (message: string) => void;
    readonly resolveCurrent: (browserSession: string) => void;
  };
  gatewayStatusNode?: Element;
}

async function expectSameStatusNode(status: Locator): Promise<void> {
  expect(
    await status.evaluate(element => element === (window as HarnessWindow).gatewayStatusNode),
  ).toBe(true);
  await expect(status).toHaveCount(1);
}

test('keeps one live region across the gateway connection lifecycle', async ({page}) => {
  await page.goto('/e2e/gateway-form-harness.html');

  const input = page.getByLabel('Live gateway URL');
  const status = page.locator('#gateway-url-message');
  await expect(status).toBeAttached();
  await expect(status).toHaveText('');
  await expect(status).toHaveAttribute('role', 'status');
  await expect(status).toHaveAttribute('aria-live', 'polite');
  await expect(status).toHaveAttribute('aria-atomic', 'true');
  await status.evaluate(element => {
    (window as HarnessWindow).gatewayStatusNode = element;
  });

  await input.fill(FIRST_CAPABILITY);
  await page.getByRole('button', {name: 'Connect'}).click();
  await expect(status).toHaveText('Connecting…');
  await expect(input).toHaveAttribute('aria-describedby', 'gateway-url-message');
  await expect(input).not.toHaveAttribute('aria-invalid', 'true');
  await expectSameStatusNode(status);

  await page.getByRole('button', {name: 'Cancel'}).click();
  await expect(status).toHaveText(CANCELLED);
  await expect(status).toHaveClass('gateway-connect-status');
  await expect(input).toHaveAttribute('aria-describedby', 'gateway-url-message');
  await expect(input).not.toHaveAttribute('aria-invalid', 'true');
  await expectSameStatusNode(status);

  await input.fill(SECOND_CAPABILITY);
  await expect(status).toHaveText('');
  expect(await input.getAttribute('aria-describedby')).toBeNull();
  expect(await input.getAttribute('aria-invalid')).toBeNull();
  await expectSameStatusNode(status);

  await page.getByRole('button', {name: 'Connect'}).click();
  await expect(status).toHaveText('Connecting…');
  await page.evaluate(message => {
    (window as HarnessWindow).gatewayHarness.rejectCurrent(message);
  }, FAILURE);
  await expect(status).toHaveText(FAILURE);
  await expect(status).toHaveClass('gateway-connect-error');
  await expect(input).toHaveAttribute('aria-describedby', 'gateway-url-message');
  await expect(input).toHaveAttribute('aria-invalid', 'true');
  await expectSameStatusNode(status);

  await input.fill(THIRD_CAPABILITY);
  await expect(status).toHaveText('');
  await page.getByRole('button', {name: 'Connect'}).click();
  await page.evaluate(message => {
    (window as HarnessWindow).gatewayHarness.rejectCurrent(message);
  }, FAILURE);
  await expect(status).toHaveText(FAILURE);
  await page.getByRole('button', {name: 'Retry'}).click();
  await expect(status).toHaveText('Connecting…');
  await expect(input).not.toHaveAttribute('aria-invalid', 'true');
  await expectSameStatusNode(status);

  await page.evaluate(() => {
    (window as HarnessWindow).gatewayHarness.resolveCurrent('browser-session');
  });
  await expect(page.getByRole('button', {name: 'Connected'})).toBeDisabled();
  await expect(status).toHaveText('');
  expect(await input.getAttribute('aria-describedby')).toBeNull();
  expect(await input.getAttribute('aria-invalid')).toBeNull();
  await expectSameStatusNode(status);
});
