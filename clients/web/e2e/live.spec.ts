import {expect, test} from '@playwright/test';
import {startLiveGateway} from './gateway.js';

test('renders a recorded run through the live WebSocket gateway', async ({page}) => {
  const gateway = startLiveGateway();
  try {
    const sockets: string[] = [];
    const pageErrors: string[] = [];
    page.on('websocket', socket => sockets.push(socket.url()));
    page.on('pageerror', error => pageErrors.push(error.message));

    await page.goto(gateway.url);
    await expect(page.getByRole('heading', {name: 'round-2'})).toBeVisible();
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
        return (
          parsed.protocol === 'ws:' && parsed.pathname === '/ws' && parsed.searchParams.has('token')
        );
      }),
    ).toBe(true);
    expect(pageErrors).toEqual([]);
    await page.screenshot({path: 'artifacts/web-live.png', fullPage: true});

    expect(gateway.stop().status).toBe(0);
  } finally {
    // Read in `finally`, not in the body: `stop()` memoizes, so this is the
    // same result the body saw, and annotating here reports teardown errors
    // even when a body assertion failed first.
    const stopped = gateway.stop();
    if (stopped.errors.length > 0) {
      test.info().annotations.push({type: 'teardown', description: stopped.errors.join('; ')});
    }
  }
});
