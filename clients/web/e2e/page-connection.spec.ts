import {createServer} from 'node:net';
import {expect, test} from '@playwright/test';
import {
  CONTROLS_BANNER_COPY,
  LIVE_SESSION_HEADER_COPY,
  PAGE_CONNECTION_BANNER_COPY,
} from '../src/banners.js';

test('identifies a live page whose gateway is unreachable on cold start', async ({page}) => {
  const gateway = await rejectingLoopbackGateway();
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));

  try {
    await page.goto(`/?gateway=${encodeURIComponent(gateway.url)}`);
    await gateway.rejected;

    const pageConnection = page.getByTestId('page-connection-banner');
    await expect(pageConnection).toHaveText(PAGE_CONNECTION_BANNER_COPY.cold);
    await expect(
      page.getByRole('heading', {name: LIVE_SESSION_HEADER_COPY.unreachable.heading}),
    ).toBeVisible();
    await expect(page.locator('.status')).toHaveText(LIVE_SESSION_HEADER_COPY.unreachable.status);

    // The page-level diagnosis does not repurpose or replace the command-path
    // diagnosis. Both name their own scope, and replay mode is not involved.
    await expect(page.getByTestId('controls-banner')).toContainText(CONTROLS_BANNER_COPY.cold);
    await expect(page.getByTestId('replay-banner')).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  } finally {
    await gateway.close();
  }
});

/** Accept one real TCP dial, reject it, and leave no gateway listening. */
async function rejectingLoopbackGateway(): Promise<{
  url: string;
  rejected: Promise<void>;
  close: () => Promise<void>;
}> {
  let resolveRejection: () => void = () => {};
  let rejectRejection: (reason: unknown) => void = () => {};
  const rejected = new Promise<void>((resolve, reject) => {
    resolveRejection = resolve;
    rejectRejection = reject;
  });
  let closePromise: Promise<void> | null = null;
  const server = createServer(socket => {
    socket.destroy();
    void close().then(resolveRejection, rejectRejection);
  });
  const close = (): Promise<void> => {
    if (closePromise !== null) return closePromise;
    closePromise = new Promise<void>((resolve, reject) => {
      if (!server.listening) {
        resolve();
        return;
      }
      server.close(error => (error === undefined ? resolve() : reject(error)));
    });
    return closePromise;
  };
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  if (address === null || typeof address === 'string') {
    await close();
    throw new Error('Loopback listener did not allocate a TCP port');
  }
  return {url: `http://127.0.0.1:${address.port}/`, rejected, close};
}
