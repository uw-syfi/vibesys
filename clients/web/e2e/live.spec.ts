import {expect, test} from '@playwright/test';
import {withGateway} from './live-gateway.js';

/**
 * The window over a real detached gateway: the replay is served, folded, and
 * shown, on two sockets to that gateway and nothing else.
 *
 * The run window has no event counter, so what the prototype asserted as a
 * `round-2` heading and "15 folded events" is asserted here as the state those
 * numbers described: both of the replay's rounds are listed, the last one is
 * selected as the live round, and the run reads as finished. Those are folded
 * from the same stream, and each names which part of the fold produced it.
 */
test('renders a recorded run through the live WebSocket gateway', async ({page}) => {
  await withGateway(async gateway => {
    const sockets: string[] = [];
    const pageErrors: string[] = [];
    page.on('websocket', socket => sockets.push(socket.url()));
    page.on('pageerror', error => pageErrors.push(error.message));

    await page.goto(gateway.url);
    const rounds = page.getByRole('navigation', {name: 'Runs'});
    // Named by role rather than by the `r2` the row prints: the accessible name
    // carries the round's folded outcome, so this fails if the gate events the
    // round is built from did not arrive.
    await expect(rounds.getByRole('button', {name: /^Round 1, /})).toBeVisible();
    await expect(rounds.getByRole('button', {name: /^Round 2, /})).toHaveAttribute(
      'aria-current',
      'true',
    );
    // The run's terminal event was folded too. `.titlebar .status` is the one
    // place the window states the run's lifecycle; it has no role of its own.
    await expect(page.locator('.titlebar .status')).toHaveText('Completed');
    await expect(page.locator('main .sticky')).toContainText('Round 2');
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
  });
});
