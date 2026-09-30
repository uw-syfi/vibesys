import {expect, test} from '@playwright/test';
import {STREAM_BANNER_COPY} from '../src/banners.js';
import {withGateway} from './gateway.js';

/**
 * The stream banner on a run the page reaches after it finished, which is the
 * one state where the transcript can be empty and the run's own status says
 * nothing is missing.
 *
 * The page's two sockets share one URL and are told apart by their first client
 * frame, as in `controls-banner.spec.ts`: the control channel opens with
 * `query.snapshot`, the event stream with `subscribe`. Only the control socket
 * is proxied to the gateway, so the snapshot still arrives and the status chip
 * still reads `completed`; the stream's socket is refused, so no batch is ever
 * folded.
 *
 * Refused rather than closed after the batch, because the defect is about the
 * bootstrap: a drop after it leaves the caller holding the history it asked for
 * and stays silent by design. This spec and the controls spec interfere with
 * opposite sockets, and the controls harness's soundness argument is that the
 * stream's socket is never touched, so they do not share an injector.
 *
 * `live.spec.ts` is the negative of this: a healthy replay of the same fixture
 * folds all 15 events and raises no banner at all.
 */
test('states that an ended run transcript stopped short when the stream drops', async ({
  page,
  context,
}) => {
  await withGateway(async gateway => {
    await context.routeWebSocket(
      url => url.pathname === '/ws',
      ws => {
        let server: ReturnType<typeof ws.connectToServer> | null = null;
        let classified = false;
        ws.onMessage(message => {
          const frame = typeof message === 'string' ? message : message.toString();
          if (!classified) {
            classified = true;
            if (frame.includes('"subscribe"')) {
              // Before `connectToServer()`, so the gateway never subscribes and
              // no batch can arrive late and fill the fold under the assertions.
              void ws.close();
              return;
            }
            server = ws.connectToServer();
            server.onMessage(reply => ws.send(reply));
          }
          server?.send(frame);
        });
      },
    );

    await page.goto(gateway.url);
    // The chip comes from the snapshot, the transcript from the stream, so this
    // is the pair the defect put on screen together with nothing to explain it.
    await expect(page.locator('.status')).toHaveText('completed');
    await expect(page.getByText('0 folded events')).toBeVisible();
    const banner = page.getByTestId('stream-banner');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText(STREAM_BANNER_COPY.ended);
    // An ended run cannot be resubscribed, so the affordance is withheld rather
    // than offered as a no-op.
    await expect(banner.getByRole('button', {name: 'Reattach'})).toHaveCount(0);
  });
});
