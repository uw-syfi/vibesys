import {expect, test} from '@playwright/test';
import {withGateway} from './gateway.js';

test('renders a recorded run through the live WebSocket gateway', async ({page}) => {
  await withGateway(async gateway => {
    const sockets: string[] = [];
    const pageErrors: string[] = [];
    page.on('websocket', socket => sockets.push(socket.url()));
    page.on('pageerror', error => pageErrors.push(error.message));

    await page.goto(gateway.url);
    await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();
    await expect(page.getByText('15 folded events')).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
    // Named explicitly as well as counted: a control channel that reported a
    // spurious outage against the real gateway is the failure this banner would
    // introduce, and the aggregate count alone would not say which banner rose.
    await expect(page.getByTestId('controls-banner')).toHaveCount(0);
    await expect.poll(() => sockets.length).toBe(2);
    expect(
      sockets.every(url => {
        const parsed = new URL(url);
        return parsed.protocol === 'ws:' && parsed.pathname === '/ws' && parsed.search === '';
      }),
    ).toBe(true);
    expect(pageErrors).toEqual([]);
    await page.screenshot({path: 'artifacts/web-live.png', fullPage: true});
  });
});

test('removes the launch capability while preserving same-browser history', async ({
  browser,
  page,
}) => {
  await withGateway(async gateway => {
    await page.goto(gateway.url);
    await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();

    const cleanUrl = page.url();
    expect(cleanUrl).toBe(new URL('/', gateway.url).toString());
    expect(cleanUrl).not.toContain('token=');

    await page.reload();
    await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();
    await page.goto('about:blank');
    await page.goBack();
    await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();

    const freshContext = await browser.newContext();
    try {
      const freshPage = await freshContext.newPage();
      const response = await freshPage.goto(cleanUrl);
      expect(response?.status()).toBe(403);
    } finally {
      await freshContext.close();
    }
  });
});

test('uses the direct gateway cookie when session storage is denied', async ({page}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'sessionStorage', {
      configurable: true,
      get() {
        throw new DOMException('Denied by browser policy', 'SecurityError');
      },
    });
  });

  await withGateway(async gateway => {
    await page.goto(gateway.url);
    await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();
    expect(page.url()).toBe(new URL('/', gateway.url).toString());
    expect(page.url()).not.toContain('token=');
  });
});
