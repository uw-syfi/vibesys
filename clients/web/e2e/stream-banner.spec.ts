import {expect, test} from '@playwright/test';
import {EMPTY_TRANSCRIPT_COPY, STREAM_BANNER_COPY} from '../src/features/run-view/index.js';
import {withGateway} from './gateway.js';

interface EventBatch {
  readonly type?: string;
  readonly events?: readonly unknown[];
  readonly [key: string]: unknown;
}

interface StreamFault {
  run?: () => Promise<void>;
}

/**
 * Forward an ordinary reply, or reduce the first event batch to a valid prefix
 * and arm the explicit stream fault used by this scenario.
 */
function forwardBootstrapReply(
  response: string,
  truncate: boolean,
  send: (frame: string) => void,
  close: () => Promise<void>,
  streamFault: StreamFault,
): boolean {
  if (!truncate) {
    send(response);
    return false;
  }
  let batch: EventBatch;
  try {
    batch = JSON.parse(response) as EventBatch;
  } catch {
    send(response);
    return true;
  }
  if (batch.type !== 'event_batch') {
    send(response);
    return true;
  }
  const first = batch.events?.[0];
  if (first === undefined) throw new Error('The fixture bootstrap batch was empty');
  // A valid first chunk establishes the store and cursor without rendering
  // activity. The fenced snapshot can then prove that the completed run has a
  // tail this page has not folded.
  send(JSON.stringify({...batch, events: [first], through_sequence: 1}));
  streamFault.run = async () => {
    send(
      JSON.stringify({
        type: 'protocol_error',
        code: 'stream_failed',
        message: 'Injected stream failure',
      }),
    );
    await close();
  };
  return false;
}

/**
 * The stream banner on a run the page reaches after it finished, which is the
 * one state where the transcript can be empty and the run's own status says
 * nothing is missing.
 *
 * The page's two sockets share one URL and are told apart by their first client
 * frame, as in `controls-banner.spec.ts`: the event stream opens with
 * `subscribe`, then its first batch names the store for a fenced snapshot. The
 * first stream receives only the first, non-transcript event before an in-band
 * fault. The snapshot therefore still says `completed` while the event cursor
 * remains behind it and no activity is rendered. A later stream socket is
 * proxied so the rendered recovery action has a real successful outcome.
 *
 * The fault is injected only after the page has loaded the fenced terminal
 * snapshot. That ordering proves the watermark keeps the partial transcript
 * stale. It also avoids racing the stream's automatic retry: an in-band
 * protocol fault belongs to the explicit Reattach path, while the close that
 * follows it is suppressed by the transport as duplicate evidence.
 *
 * `live.spec.ts` is the negative of this: a healthy replay of the same fixture
 * folds all 15 events and raises no banner at all.
 */
test('states that an ended run transcript stopped short when the stream drops', async ({
  page,
  context,
}) => {
  await withGateway(async gateway => {
    let interceptedFirstStream = false;
    const streamFault: StreamFault = {};
    await context.routeWebSocket(
      url => url.pathname === '/ws',
      ws => {
        let server: ReturnType<typeof ws.connectToServer> | null = null;
        let classified = false;
        let truncate = false;
        ws.onMessage(message => {
          const frame = typeof message === 'string' ? message : message.toString();
          if (classified) {
            server?.send(frame);
            return;
          }
          classified = true;
          truncate = frame.includes('"subscribe"') && !interceptedFirstStream;
          if (truncate) interceptedFirstStream = true;
          server = ws.connectToServer();
          server.onMessage(reply => {
            truncate = forwardBootstrapReply(
              String(reply),
              truncate,
              frame => ws.send(frame),
              () => ws.close(),
              streamFault,
            );
          });
          server.send(frame);
        });
      },
    );

    await page.goto(gateway.url);
    // The chip comes from the snapshot, the transcript from the stream, so this
    // is the pair the defect put on screen together with nothing to explain it.
    await expect(page.locator('.status')).toHaveText('completed');
    await expect(page.getByText('0 folded events')).toBeVisible();
    expect(streamFault.run, 'the initial stream batch was never intercepted').toBeDefined();
    await streamFault.run?.();
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
