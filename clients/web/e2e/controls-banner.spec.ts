import {expect, type Locator, type Page, test} from '@playwright/test';
import {CONTROLS_BANNER_COPY} from '../src/banners.js';
import {withGateway} from './gateway.js';

/**
 * The controls banner against a real gateway, with the page's view of the run
 * held open by frame injection.
 *
 * Why injection rather than a truncated log: `--web-reopen` refuses an
 * unfinished run by design (`attach_read_only`), and `web live --demo` is a
 * replay too, so the only way to get a genuinely live run is a real project
 * run, which is far too heavy for CI. Injection keeps the log finished and
 * makes only the page's view non-terminal, which is the state the banner is
 * about.
 *
 * The page holds two sockets on one URL, told apart by their first client
 * frame: the control channel opens with `query.snapshot` (the session's
 * `start()` issues it), the event stream with `subscribe`. Both are proxied to
 * the real gateway, so the run, the capability token, and the replay keep
 * working while only the control socket is interfered with.
 *
 * Sustaining an outage: `ControlChannel`'s backoff is finite (500, 1000, 2000,
 * 4000, 8000 ms) and a single close produces a banner that lives under a
 * second, so the redials have to be refused too. They are refused on arrival,
 * before the proxy connects them onward, because a socket that is allowed to
 * open reports `connected` and then drops again, which would make the banner
 * flicker. Refusing on arrival cannot classify the socket, so it relies on the
 * stream's socket never being closed and therefore never redialing; that
 * condition is asserted rather than assumed (`streamSocketClosed`).
 *
 * No `setOffline`: an established socket stays open through it (measured
 * unchanged over 75s). No sleeps and no wall-clock; every wait is a Playwright
 * expectation.
 */

type SocketKind = 'control' | 'stream' | 'unclassified';

interface Injection {
  /** Refuse every socket opened from now on, so a reported outage lasts. */
  blockDials: boolean;
  /** Withhold the batch tail that ends the run instead of forwarding it. */
  holdTerminal: boolean;
  /** Drop the batch tail that ends the run, so the page never sees it. */
  stripTerminal: boolean;
}

interface Observed {
  /** What each socket turned out to be, in the order they were opened. */
  readonly kinds: SocketKind[];
  /** Sockets refused on arrival while `blockDials` was set. */
  refusedDials: number;
  /** Whether the socket carrying `subscribe` was ever closed toward the page. */
  streamSocketClosed: boolean;
  /** Run-ending batch tails withheld from the page. */
  readonly held: string[];
  /** Close the live control socket toward the page, as a gateway drop would. */
  closeControl: (() => Promise<void>) | null;
  /** Forward a withheld run-ending tail to the page. */
  releaseTerminal: (() => void) | null;
}

interface EventBatch {
  readonly type?: string;
  readonly events?: Array<{readonly type?: string}>;
}

/**
 * Split a batch at its run-ending event: the events before it, safe to fold
 * now, and the tail from that event on, which is what terminates the run.
 * `null` when the batch does not end the run.
 *
 * Only `events` is rewritten. `core-state` folds events in order and skips one
 * whose sequence it has already passed, so the prefix advances the cursor to
 * its own last event and the withheld tail still folds when it arrives.
 */
function splitAtTerminal(frame: string): {prefix: string | null; tail: string} | null {
  let batch: EventBatch;
  try {
    batch = JSON.parse(frame) as EventBatch;
  } catch {
    return null;
  }
  if (batch.type !== 'event_batch') return null;
  const events = batch.events ?? [];
  const index = events.findIndex(event => event.type === 'run_finished');
  if (index === -1) return null;
  return {
    prefix: index === 0 ? null : JSON.stringify({...batch, events: events.slice(0, index)}),
    tail: JSON.stringify({...batch, events: events.slice(index)}),
  };
}

/** Proxy both sockets to the gateway, classify them, and interfere on demand. */
async function injectFrames(page: Page, injection: Injection): Promise<Observed> {
  const observed: Observed = {
    kinds: [],
    refusedDials: 0,
    streamSocketClosed: false,
    held: [],
    closeControl: null,
    releaseTerminal: null,
  };
  // Matched by pathname, not by a glob. The URL is
  // `ws://127.0.0.1:<port>/ws?token=<token>`, and `'**/ws'` does not match a URL
  // with a query string, so the route never fired and both specs measured an
  // uninstrumented page. `live.spec.ts` already asserts that exact URL shape;
  // the refutation was in the sibling file the whole time.
  //
  // Registered on the context rather than the page. Both should route a redial
  // opened after `goto`, but the context form is the one that has actually been
  // measured sustaining an outage across a series of refused dials here, and
  // choosing the variant with evidence costs nothing.
  await page.context().routeWebSocket(
    url => url.pathname === '/ws',
    ws => {
      if (injection.blockDials) {
        // Refused before `connectToServer()`, so the page's socket never opens
        // and the channel records a failed dial rather than a connect-then-drop.
        observed.refusedDials += 1;
        void ws.close();
        return;
      }
      const index = observed.kinds.length;
      observed.kinds.push('unclassified');
      const server = ws.connectToServer();
      let kind: SocketKind = 'unclassified';
      ws.onMessage(message => {
        if (kind === 'unclassified') {
          kind = String(message).includes('"query.snapshot"') ? 'control' : 'stream';
          observed.kinds[index] = kind;
          if (kind === 'control') observed.closeControl = () => ws.close();
        }
        server.send(message);
      });
      server.onMessage(message => {
        const split = kind === 'stream' ? splitAtTerminal(String(message)) : null;
        if (split === null || !(injection.holdTerminal || injection.stripTerminal)) {
          ws.send(message);
          return;
        }
        if (split.prefix !== null) ws.send(split.prefix);
        if (injection.stripTerminal) return;
        observed.held.push(split.tail);
        observed.releaseTerminal = () => ws.send(split.tail);
      });
      ws.onClose(() => {
        if (kind === 'stream') observed.streamSocketClosed = true;
      });
    },
  );
  return observed;
}

/**
 * Fail fast, and say why, when the page is not running this checkout's code.
 *
 * The gateway serves `clients/web/dist`, a `vite build` output, so editing
 * `clients/web/src` changes nothing the browser runs until that is rebuilt.
 * `gateway.ts` now requires the bundle to exist and the `test:e2e` script builds
 * it, but a run that reaches the gateway by another route can still serve an old
 * one, and the failure mode is the worst available: the page served copy that
 * `ad153813` had deleted, so this regression spec reported its defect as still
 * present at a head where it was fixed.
 *
 * The expected text is imported from `../src/banners.js`, so this is an identity
 * check against the module under test rather than against a string a spec author
 * retyped. Asserted before any behavior assertion, so a stale bundle cannot
 * present as a behavior failure.
 */
async function expectBundleUnderTest(banner: Locator): Promise<void> {
  await expect(
    banner,
    'the dev server is serving a different build of clients/web: stop any vite on 5173 and re-run',
  ).toContainText(CONTROLS_BANNER_COPY.lost);
}

/** Drop the control socket and refuse its redials, leaving the stream alone. */
async function breakControlChannel(observed: Observed, injection: Injection): Promise<void> {
  const close = observed.closeControl;
  expect(close, 'the control socket was never identified').not.toBeNull();
  injection.blockDials = true;
  await close?.();
}

test('reports a dead control channel without disturbing the live transcript', async ({page}) => {
  await withGateway(async gateway => {
    const injection: Injection = {blockDials: false, holdTerminal: false, stripTerminal: true};
    const observed = await injectFrames(page, injection);
    const pageErrors: string[] = [];
    page.on('pageerror', error => pageErrors.push(error.message));

    const controls = page.getByTestId('controls-banner');
    const stream = page.getByTestId('stream-banner');
    const transcript = page.getByText(/folded events/);

    // The run-ending event is stripped, so the page's run never terminates and
    // the banner is not withheld as a finished run's.
    await page.goto(gateway.url);
    await expect(page.getByRole('heading', {name: 'round-2'})).toBeVisible();
    await expect(transcript).toBeVisible();
    await expect(controls).toHaveCount(0);
    await expect(stream).toHaveCount(0);
    const foldedBefore = await transcript.textContent();
    expect(observed.kinds).toEqual(['control', 'stream']);

    await breakControlChannel(observed, injection);

    await expect(controls).toBeVisible();
    // The page had a connection and lost it, so the copy is the `lost` case, and
    // it names the consequence this client actually has rather than four run
    // controls it does not implement yet. `banners.test.ts` owns which case a
    // given state selects; what this pins is that the page renders it.
    await expectBundleUnderTest(controls);
    await expect(controls.getByRole('button', {name: /Reconnect now|Reconnecting/})).toBeVisible();

    // Awaited, not read. `ControlChannel.#onDrop` reports the outage and *then*
    // arms the redial, so the banner is on screen in the same task as the close
    // while the first dial is still 500ms away (`DEFAULT_RECONNECT_DELAYS_MS`).
    // Every assertion above settles on its first poll, so a synchronous read
    // here lands inside that 500ms and reports zero every time: the counter was
    // right and the spec was early.
    //
    // Two, not one, because the claim this spec makes is that the outage is
    // sustained by refusing the *series*. One refusal is consistent with a
    // single dial that happened to be caught. Refused dials fail as fast as the
    // route can close them, so the second arrives about 1.5s in; the timeout is
    // generous against the 500/1000/2000/4000/8000 schedule rather than tuned.
    await expect.poll(() => observed.refusedDials, {timeout: 10_000}).toBeGreaterThanOrEqual(2);

    // Checked after the refusal series, so they hold across the whole outage
    // rather than in the instant after the close. A dead command path with a
    // live transcript is the case the two separate reports exist for, so the
    // stream must say nothing about this.
    await expect(controls).toBeVisible();
    await expect(stream).toHaveCount(0);
    await expect(page.getByRole('heading', {name: 'round-2'})).toBeVisible();
    expect(await transcript.textContent()).toBe(foldedBefore);
    // Refusing every dial indiscriminately is only sound while the stream never
    // redials, which would otherwise be refused too. Now covers the series.
    expect(observed.streamSocketClosed).toBe(false);
    expect(pageErrors).toEqual([]);
  });
});

/**
 * The regression for the latched banner. The terminal event is withheld rather
 * than stripped, so the run can be ended on demand *after* the outage is
 * already on screen, which is the ordering the two independent sockets allow
 * and the suite never exercised: the control channel reports its drop while the
 * run's last batch is still in flight.
 *
 * The banner must come down when the run ends. It cannot come down because the
 * channel recovered, because the dials stay refused for the whole test, so the
 * only thing that can clear it is the run's own terminal event being taken into
 * account. At `e723bf75` the decision was latched at report time and this fails.
 */
test('takes the controls banner down when the run ends during the outage', async ({page}) => {
  await withGateway(async gateway => {
    const injection: Injection = {blockDials: false, holdTerminal: true, stripTerminal: false};
    const observed = await injectFrames(page, injection);
    const pageErrors: string[] = [];
    page.on('pageerror', error => pageErrors.push(error.message));

    const controls = page.getByTestId('controls-banner');
    const status = page.locator('.status');

    await page.goto(gateway.url);
    await expect(page.getByRole('heading', {name: 'round-2'})).toBeVisible();
    // The run-ending tail is withheld, not merely late: assert it was actually
    // intercepted before relying on the page's run being unfinished.
    await expect.poll(() => observed.held.length).toBeGreaterThan(0);
    await expect(status).not.toHaveText('completed');
    await expect(controls).toHaveCount(0);

    await breakControlChannel(observed, injection);
    await expect(controls).toBeVisible();
    // Before the assertion this spec exists for: a banner rendered by a stale
    // bundle would fail to come down for a reason that has nothing to do with
    // the fix, and the failure would point at the wrong thing.
    await expectBundleUnderTest(controls);

    // Awaited before the release, because this spec's argument is that the only
    // thing which can take the banner down is the run ending. That argument
    // needs the channel to be demonstrably still trying and still failing. Read
    // synchronously, this was zero (the first redial is 500ms out, and every
    // assertion above settles sooner), which made the comparison below
    // `0 >= 0`: true whatever happened, and therefore evidence of nothing.
    await expect.poll(() => observed.refusedDials, {timeout: 10_000}).toBeGreaterThanOrEqual(1);
    const refusedBefore = observed.refusedDials;
    expect(refusedBefore).toBeGreaterThanOrEqual(1);
    observed.releaseTerminal?.();

    await expect(status).toHaveText('completed');
    await expect(controls).toHaveCount(0);
    // The channel is still dead and was still being refused across the release,
    // so the banner came down because the run ended and for no other reason.
    expect(observed.refusedDials).toBeGreaterThanOrEqual(refusedBefore);
    expect(observed.streamSocketClosed).toBe(false);
    expect(pageErrors).toEqual([]);
  });
});
