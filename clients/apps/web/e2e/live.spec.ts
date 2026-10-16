import {expect, test} from '@playwright/test';
import {matrixTest, withGateway} from './gateway.js';

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
  const sockets: string[] = [];
  page.on('websocket', socket => sockets.push(socket.url()));
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
    await expect(page.getByLabel('Live gateway URL')).toHaveCount(0);
    const cleanUrl = new URL('/', gateway.url).toString();
    expect(page.url()).toBe(cleanUrl);
    expect(page.url()).not.toContain('token=');
    expect(await page.evaluate(() => window.history.state)).toEqual({
      vibesysGateway: {kind: 'direct-gateway', version: 1, gatewayUrl: cleanUrl},
    });
    await expect.poll(() => sockets.length).toBe(2);

    await page.reload();
    await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();
    await expect(page.getByLabel('Live gateway URL')).toHaveCount(0);
    await expect.poll(() => sockets.length).toBe(4);

    await page.goto('about:blank');
    await page.goBack();
    await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();
    await expect(page.getByLabel('Live gateway URL')).toHaveCount(0);
    expect(page.url()).toBe(cleanUrl);
    await expect.poll(() => sockets.length).toBe(6);
    expect(
      sockets.every(url => {
        const parsed = new URL(url);
        return parsed.pathname === '/ws' && parsed.search === '';
      }),
    ).toBe(true);
  });
});

test('keeps a manually exchanged session when storage is denied across loopback sites', async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'sessionStorage', {
      configurable: true,
      get() {
        throw new DOMException('Denied by browser policy', 'SecurityError');
      },
    });
  });

  await withGateway(
    async gateway => {
      const sockets: string[] = [];
      const capabilityRequests: string[] = [];
      page.on('websocket', socket => sockets.push(socket.url()));
      page.on('request', request => {
        if (new URL(request.url()).searchParams.has('token')) {
          capabilityRequests.push(request.url());
        }
      });
      const capability = new URL(gateway.url);
      capability.hostname = 'localhost';

      await page.goto('/');
      await page.getByRole('button', {name: 'Connect', exact: true}).click();
      await page.getByLabel('Live gateway URL').fill(capability.toString());
      await page.locator('form.gateway-connect').evaluate(form => {
        form.dispatchEvent(new SubmitEvent('submit', {bubbles: true, cancelable: true}));
        form.dispatchEvent(new SubmitEvent('submit', {bubbles: true, cancelable: true}));
      });

      await expect(page.getByLabel('Live gateway URL')).toHaveCount(0);
      await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();
      const gatewaySockets = (): URL[] =>
        sockets.map(url => new URL(url)).filter(socket => socket.pathname === '/ws');
      await expect.poll(() => gatewaySockets().length).toBeGreaterThan(0);
      expect(capabilityRequests).toEqual([capability.toString()]);
      expect(page.url()).not.toContain('token=');
      expect(page.url()).not.toContain('session=');
      expect(
        gatewaySockets().every(socket => {
          return socket.hostname === 'localhost' && socket.searchParams.has('session');
        }),
      ).toBe(true);
    },
    ['http://127.0.0.1:5173'],
  );
});

test('keeps a run-scoped draft and shows the gateway answer while guarding terminal steering', async ({
  page,
}) => {
  await withGateway(async gateway => {
    await page.goto(gateway.url);
    await expect(page.getByRole('heading', {name: 'Run overview'})).toBeVisible();
    const launcher = page.getByRole('button', {name: 'Conversation'});
    await launcher.click();
    const dialog = page.getByRole('dialog', {name: 'Conversation'});
    await expect(dialog.getByText(/^Run /)).toBeVisible();
    const question = dialog.getByRole('textbox', {name: 'Question about this run'});
    await question.fill('What happened in this recorded run?');
    await dialog.getByRole('button', {name: 'Close conversation'}).click();
    await expect(launcher).toBeFocused();
    await launcher.click();
    await expect(question).toHaveValue('What happened in this recorded run?');
    await question.press('Shift+Enter');
    await expect(question).toHaveValue(/\n/);
    await question.press('Enter');
    await expect(dialog).toContainText('What happened in this recorded run?');
    await expect(dialog).toContainText('read-only summary from the recorded events');
    await expect(question).toBeEnabled();

    await dialog.getByRole('button', {name: 'Steer next invocation'}).click();
    const guidance = dialog.getByRole('textbox', {name: 'Guidance for the next invocation'});
    await expect(guidance).toBeDisabled();
    await expect(dialog).toContainText('This run has ended. There is no next invocation to steer.');
    await expect(dialog.getByRole('button', {name: 'Steer', exact: true})).toBeDisabled();
    await page.keyboard.press('Escape');
    await expect(launcher).toBeFocused();
  });
});

matrixTest(
  'Steer remains usable while Ask waits, with a separate draft for each intent',
  async ({page, context, matrixGateways}) => {
    const gateway = await matrixGateways.start('conversation-draft');
    let chatRequestId: string | null = null;
    const held = {
      answer: null as string | null,
      deliver: null as ((frame: string) => void) | null,
    };
    await context.routeWebSocket(
      url => url.pathname === '/ws',
      ws => {
        let server: ReturnType<typeof ws.connectToServer> | null = null;
        ws.onMessage(message => {
          const frame = String(message);
          if (server === null) {
            server = ws.connectToServer();
            server.onMessage(reply => {
              const response = JSON.parse(String(reply)) as {request_id?: string};
              if (chatRequestId !== null && response.request_id === chatRequestId) {
                held.answer = String(reply);
                held.deliver = answer => ws.send(answer);
              } else {
                ws.send(reply);
              }
            });
          }
          const request = JSON.parse(frame) as {type?: string; request_id?: string};
          if (request.type === 'query.chat') chatRequestId = request.request_id ?? null;
          server.send(frame);
        });
      },
    );

    await page.goto(gateway.url);
    await expect(page.locator('.status')).toContainText('running');
    const launcher = page.getByRole('button', {name: 'Conversation'});
    await launcher.click();
    const dialog = page.getByRole('dialog', {name: 'Conversation'});
    const question = dialog.getByRole('textbox', {name: 'Question about this run'});
    await question.fill('What happened?');
    await question.press('Enter');
    await expect.poll(() => held.answer !== null).toBe(true);
    await expect(dialog).toContainText('Waiting for a response…');
    await dialog.getByRole('button', {name: 'Close conversation'}).click();
    await expect(launcher).toBeFocused();
    await launcher.click();
    await expect(dialog).toContainText('Waiting for a response…');

    await question.fill('What should we try next?');
    await expect(dialog.getByRole('button', {name: 'Waiting…'})).toBeDisabled();
    await dialog.getByRole('button', {name: 'Steer next invocation'}).click();
    const guidance = dialog.getByRole('textbox', {name: 'Guidance for the next invocation'});
    await expect(guidance).toBeEnabled();
    await expect(guidance).toHaveValue('');
    await guidance.fill('Prioritize latency');
    await dialog.getByRole('button', {name: 'Ask about this run'}).click();
    await expect(question).toHaveValue('What should we try next?');
    await dialog.getByRole('button', {name: 'Steer next invocation'}).click();
    await expect(guidance).toHaveValue('Prioritize latency');
    await dialog.getByRole('button', {name: 'Steer', exact: true}).click();
    // The fixture has no installed run control; its internal reason is sanitized at the wire boundary.
    await expect(dialog).toContainText('Steering failed: Request failed');
    await expect(dialog).toContainText('Waiting for a response…');

    if (held.answer === null || held.deliver === null)
      throw new Error('Chat response was not held');
    held.deliver(held.answer);
    await expect(dialog).toContainText('experiment chat agent is not available');
    await dialog.getByRole('button', {name: 'Ask about this run'}).click();
    await expect(question).toHaveValue('What should we try next?');
  },
);

test('preserves multiline transcript text within the minimum viewport', async ({page}) => {
  const formattedContent = `one
  two
${'A'.repeat(512)}`;
  const baselineContent = `one
  two
short`;
  let content = baselineContent;
  await page.routeWebSocket(
    url => url.pathname === '/ws',
    socket => {
      const server = socket.connectToServer();
      server.onMessage(message => {
        const frame = JSON.parse(String(message)) as {
          type?: string;
          events?: Array<Record<string, unknown>>;
        };
        if (frame.type !== 'event_batch' || frame.events === undefined) {
          socket.send(message);
          return;
        }
        const events = frame.events.map(event =>
          event['sequence'] === 15
            ? {
                ...event,
                type: 'agent_output_chunk',
                status: null,
                agent_kind: 'implementer',
                invocation_id: 'formatting-invocation',
                execution_id: 'formatting-execution',
                data: {kind: 'agent_output_chunk', channel: 'assistant', content},
              }
            : event,
        );
        socket.send(JSON.stringify({...frame, events}));
      });
    },
  );
  await page.setViewportSize({width: 320, height: 640});
  await withGateway(async gateway => {
    await page.goto(gateway.url);
    await expect(page.locator('.transcript-content').filter({hasText: 'short'})).toHaveText(
      baselineContent,
    );
    // The transcript can show while the header still reads "Connecting to the server", which
    // is wider at this viewport, so each width is read only once the run's final status shows.
    await expect(page.locator('.status')).toHaveText('completed');
    const baselineDocumentWidth = await page.evaluate(() => document.documentElement.scrollWidth);

    content = formattedContent;
    await page.reload();

    const transcript = page.locator('.transcript-content').filter({hasText: 'A'.repeat(128)});
    await expect(transcript).toHaveText(formattedContent);
    await expect(page.locator('.status')).toHaveText('completed');
    const layout = await transcript.evaluate((element, payload) => {
      const text = element.querySelector('pre')?.firstChild;
      const entry = element.parentElement;
      const panel = element.closest('.panel');
      if (!(text instanceof Text) || entry === null || panel === null) {
        throw new Error('Transcript markup does not expose its text row and panel');
      }
      const character = (index: number): DOMRect => {
        const range = document.createRange();
        range.setStart(text, index);
        range.setEnd(text, index + 1);
        return range.getBoundingClientRect();
      };
      const first = character(payload.indexOf('one'));
      const second = character(payload.indexOf('two'));
      const style = getComputedStyle(element);
      return {
        documentWidth: document.documentElement.scrollWidth,
        entryClientWidth: entry.clientWidth,
        entryScrollWidth: entry.scrollWidth,
        firstLeft: first.left,
        firstTop: first.top,
        overflowWrap: style.overflowWrap,
        panelClientWidth: panel.clientWidth,
        panelScrollWidth: panel.scrollWidth,
        secondLeft: second.left,
        secondTop: second.top,
        whiteSpace: style.whiteSpace,
      };
    }, formattedContent);

    expect(layout.whiteSpace).toBe('pre-wrap');
    expect(layout.overflowWrap).toBe('anywhere');
    expect(layout.secondTop).toBeGreaterThan(layout.firstTop);
    expect(layout.secondLeft).toBeGreaterThan(layout.firstLeft);
    expect(layout.entryScrollWidth).toBeLessThanOrEqual(layout.entryClientWidth);
    expect(layout.panelScrollWidth).toBeLessThanOrEqual(layout.panelClientWidth);
    expect(layout.documentWidth).toBe(baselineDocumentWidth);
  });
});
