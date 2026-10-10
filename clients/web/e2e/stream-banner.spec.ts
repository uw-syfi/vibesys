import {expect, test} from '@playwright/test';
import {EMPTY_TRANSCRIPT_COPY, STREAM_BANNER_COPY} from '../src/banners.js';
import {withGateway} from './gateway.js';

/**
 * The stream banner on a run the page reaches after it finished, which is the
 * one state where the transcript can be empty and the run's own status says
 * nothing is missing.
 *
 * The page's two sockets share one URL and are told apart by their first client
 * frame, as in `controls-banner.spec.ts`: the control channel opens with
 * `query.snapshot`, the event stream with `subscribe`. The first stream socket
 * is refused, so the snapshot still arrives, the status chip still reads
 * `completed`, and no batch is folded. A later stream socket is proxied so the
 * rendered recovery action has a real successful outcome.
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
    let refusedFirstStream = false;
    await context.routeWebSocket(
      url => url.pathname === '/ws',
      ws => {
        let server: ReturnType<typeof ws.connectToServer> | null = null;
        let classified = false;
        ws.onMessage(message => {
          const frame = typeof message === 'string' ? message : message.toString();
          if (classified) {
            server?.send(frame);
            return;
          }
          classified = true;
          if (frame.includes('"subscribe"') && !refusedFirstStream) {
            refusedFirstStream = true;
            // Before `connectToServer()`, so the gateway never subscribes and
            // no batch can arrive late under the fault assertions. A user
            // recovery gets a second socket, which is proxied below.
            void ws.close();
            return;
          }
          server = ws.connectToServer();
          server.onMessage(reply => ws.send(reply));
          server.send(frame);
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
    // The empty panel must describe the same unavailable stream, not promise
    // that an ended run will eventually replay activity into it.
    await expect(page.locator('.empty')).toHaveText(EMPTY_TRANSCRIPT_COPY.unavailable);
    // Recovery is based on the observed stream fault, not suppressed by the
    // terminal chip. The click refreshes the snapshot and opens a fresh
    // bootstrap, which this harness lets through.
    const reattach = banner.getByRole('button', {name: 'Reattach'});
    await expect(reattach).toBeVisible();
    await reattach.click();
    await expect(banner).toHaveCount(0);
    await expect(page.getByText('15 folded events')).toBeVisible();
    await expect(page.locator('.status')).toHaveText('completed');
  });
});
