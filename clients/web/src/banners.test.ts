import {describe, expect, test} from 'bun:test';
import {BackendClientError, type RunEvent} from '@vibesys/backend-client';
import {type CoreRunStatus, hasRunEnded, initialCoreState} from '@vibesys/core-state';
import {CONTROLS_BANNER_COPY, connectionBanners, STREAM_BANNER_COPY} from './banners.js';
import type {WebSessionState, WebSessionStatus} from './session.js';
import {createCoreStateStore} from './store.js';

/** Every status core state can hold, so a new one is a compile error here. */
const RUN_STATUSES: readonly CoreRunStatus[] = [
  'connecting',
  'starting',
  'running',
  'pausing',
  'paused',
  'stopping',
  'stopped',
  'completed',
  'failed',
  'interrupted',
];
const SESSION_STATUSES: readonly WebSessionStatus[] = ['connecting', 'connected', 'stale'];

const runWith = (status: CoreRunStatus) => ({...initialCoreState(), status});
const outage = new BackendClientError('disconnected', 'Server disconnected');

// Imported rather than retyped. What this file pins is which of the two a given
// state selects, and the properties the text has to hold (below); retyping the
// strings would only pin that someone copied them correctly, and a second copy
// is what let the e2e spec drift onto text the source no longer produced.
const {lost: LOST, cold: COLD} = CONTROLS_BANNER_COPY;
const {live: GAP_OPEN, ended: GAP_FINAL} = STREAM_BANNER_COPY;

function sessionState(
  status: WebSessionStatus,
  controls: 'up' | 'lost' | 'lost-retrying' | 'cold',
): WebSessionState {
  return {
    status,
    error: status === 'stale' ? outage : null,
    controls:
      controls === 'up'
        ? {status: 'connected'}
        : {
            status: 'disconnected',
            error: outage,
            everConnected: controls !== 'cold',
            retrying: controls === 'lost-retrying',
          },
  };
}

/**
 * The two events that bracket a run, shaped as the gateway sends them (the
 * `framework-events.jsonl` replay the browser specs use carries exactly these at
 * sequences 1 and 18). Declared here rather than read from
 * `clients/tui/dev/fixtures/`, which `web/src` must not depend on.
 */
const RUN_STARTED: RunEvent = {
  protocol_version: 1,
  sequence: 1,
  run_id: 'run-1',
  timestamp: '2026-09-12T10:15:00Z',
  type: 'run_started',
  text: '',
  status: 'active',
  data: {kind: 'run_started', outer_loop: 'iterate', input: 'objective.md', max_rounds: 2},
};
const RUN_FINISHED: RunEvent = {
  protocol_version: 1,
  sequence: 18,
  run_id: 'run-1',
  timestamp: '2026-09-12T10:15:17Z',
  type: 'run_finished',
  text: '',
  status: 'completed',
};

describe('connectionBanners', () => {
  test('takes the banner down on the run-ending event itself, folded by the real reducer', () => {
    // The property below builds its `CoreState` by assigning `status`, so it
    // asserts that the decision is right about a status without asserting that
    // any real input produces that status. This closes that: the run's own
    // terminal event goes through `core-state`'s reducer, which is the path the
    // browser takes, and is the same event `e2e/controls-banner.spec.ts`
    // withholds and then releases.
    const store = createCoreStateStore();
    const lostChannel = sessionState('connected', 'lost');

    store.append([RUN_STARTED]);
    expect(hasRunEnded(store.getState())).toBe(false);
    expect(connectionBanners(store.getState(), lostChannel).controls).toEqual({
      message: LOST,
      retrying: false,
    });

    store.append([RUN_FINISHED]);
    // Both facts come off the same folded state, which is why a render cannot
    // show one without the other: the status the header reads and the predicate
    // the banner reads are the same field of the same value.
    expect(store.getState().status).toBe('completed');
    expect(hasRunEnded(store.getState())).toBe(true);
    expect(connectionBanners(store.getState(), lostChannel).controls).toBeNull();

    // The stream banner crosses the same transition the other way: the gap it
    // names outlives the run, and what changes is that it can no longer close.
    const staleStream = sessionState('stale', 'up');
    expect(connectionBanners(store.getState(), staleStream).stream).toEqual({
      message: GAP_FINAL,
      reattach: false,
    });
  });

  test('names losing a connection and never having one as the different failures they are', () => {
    expect(connectionBanners(runWith('running'), sessionState('connected', 'lost'))).toEqual({
      stream: null,
      controls: {message: LOST, retrying: false},
    });
    expect(connectionBanners(runWith('running'), sessionState('connected', 'cold'))).toEqual({
      stream: null,
      controls: {message: COLD, retrying: false},
    });
  });

  test('names no operation this client cannot perform', () => {
    // `query.snapshot` is the only request `clients/web` issues, so promising
    // that pause, resume, steer, and chat will not be delivered would describe
    // affordances the page does not have. They arrive with #815.
    for (const controls of ['lost', 'cold'] as const) {
      const banner = connectionBanners(runWith('running'), sessionState('connected', controls));
      expect(banner.controls?.message).not.toContain('Pause');
      expect(banner.controls?.message).not.toContain('chat');
      // Nor does it leak the transport's own wording onto the page.
      expect(banner.controls?.message).not.toContain(outage.message);
    }
  });

  test('marks the reconnect affordance dead while a dial is already in flight', () => {
    expect(
      connectionBanners(runWith('running'), sessionState('connected', 'lost-retrying')).controls,
    ).toEqual({message: LOST, retrying: true});
  });

  test('withholds the controls banner once the run has ended', () => {
    expect(connectionBanners(runWith('completed'), sessionState('connected', 'lost'))).toEqual({
      stream: null,
      controls: null,
    });
  });

  /**
   * The regression for the latched banner: the session keeps reporting the
   * outage it observed, and the decision has to change when the run ends even
   * though the control channel never speaks again. Deciding at report time
   * cannot do this, because the two facts arrive over independent sockets and
   * nothing re-enters the session when the later one lands.
   */
  test('takes the controls banner down when the run ends after the outage', () => {
    const down = sessionState('connected', 'lost');
    expect(connectionBanners(runWith('running'), down).controls).not.toBeNull();
    expect(connectionBanners(runWith('completed'), down).controls).toBeNull();
  });

  /**
   * The #1044 render: an ended run's stream fault is a different statement
   * from an ended run's control outage, which is why one banner survives the
   * run ending and the other does not. A transcript that stopped short stays
   * wrong once the run is over, and nothing will close the gap, so the copy
   * says so and the `Reattach` that would be a no-op is withheld. A command
   * path nobody can reach stops costing the reader anything, so it goes silent.
   */
  test('keeps the stream banner on an ended run, as a gap that will not close', () => {
    expect(connectionBanners(runWith('completed'), sessionState('stale', 'up'))).toEqual({
      stream: {message: GAP_FINAL, reattach: false},
      controls: null,
    });
    expect(connectionBanners(runWith('running'), sessionState('stale', 'up'))).toEqual({
      stream: {message: GAP_OPEN, reattach: true},
      controls: null,
    });
    // The same ended run, with the control channel down instead of the stream:
    // the opposite verdict, from the same `hasRunEnded`.
    expect(connectionBanners(runWith('completed'), sessionState('stale', 'lost'))).toEqual({
      stream: {message: GAP_FINAL, reattach: false},
      controls: null,
    });
  });

  test('reports the two failures independently', () => {
    expect(connectionBanners(runWith('running'), sessionState('stale', 'lost'))).toEqual({
      stream: {message: GAP_OPEN, reattach: true},
      controls: {message: LOST, retrying: false},
    });
  });

  /**
   * Exhaustive over the closed input space (10 run statuses x 3 session
   * statuses x 4 channel states), so the properties hold for every combination
   * rather than the handful named above.
   */
  test('holds its properties for every run, stream, and channel combination', () => {
    for (const runStatus of RUN_STATUSES) {
      for (const sessionStatus of SESSION_STATUSES) {
        for (const controls of CHANNEL_STATES) {
          checkCombination(runStatus, sessionStatus, controls);
        }
      }
    }
  });
});

const CHANNEL_STATES = ['up', 'lost', 'lost-retrying', 'cold'] as const;

/** The properties one point of the input space must satisfy. */
function checkCombination(
  runStatus: CoreRunStatus,
  sessionStatus: WebSessionStatus,
  controls: (typeof CHANNEL_STATES)[number],
): void {
  const run = runWith(runStatus);
  const ended = hasRunEnded(run);
  const banners = connectionBanners(run, sessionState(sessionStatus, controls));
  const where = {runStatus, sessionStatus, controls};
  const down = controls !== 'up';

  // A banner appears exactly when the failure it names is being reported.
  // Whether the stream's appears is independent of the run's status (only what
  // it says below depends on it): a transcript that stopped short is short
  // whatever status sits above it, and on an ended run this banner is the only
  // thing on the page that says so.
  expect({...where, shown: banners.stream !== null}).toEqual({
    ...where,
    shown: sessionStatus === 'stale',
  });
  // The controls' does, and only while the run can still act on a reconnect.
  expect({...where, shown: banners.controls !== null}).toEqual({...where, shown: down && !ended});
  // No affordance is offered for a run that cannot act on it. Both live inside
  // the banner that explains them, so an orphaned button is unrepresentable.
  expect({
    ...where,
    offeredOnEnded: ended && (banners.stream?.reattach === true || banners.controls !== null),
  }).toEqual({...where, offeredOnEnded: false});
  if (banners.stream !== null) {
    // The copy names whether the gap can still close, which is what decides
    // the reader's options, and it never shows the transport's own string.
    expect({...where, message: banners.stream.message}).toEqual({
      ...where,
      message: ended ? GAP_FINAL : GAP_OPEN,
    });
    expect({...where, leaked: banners.stream.message.includes(outage.message)}).toEqual({
      ...where,
      leaked: false,
    });
    // `Reattach` is offered exactly when `WebSession.reattach` would act.
    expect({...where, reattach: banners.stream.reattach}).toEqual({...where, reattach: !ended});
  }
  if (banners.controls === null) return;
  // The copy describes the failure the channel reported, says nothing this
  // client cannot do, and never shows the transport's own string.
  expect({...where, message: banners.controls.message}).toEqual({
    ...where,
    message: controls === 'cold' ? COLD : LOST,
  });
  expect({...where, leaked: banners.controls.message.includes(outage.message)}).toEqual({
    ...where,
    leaked: false,
  });
  // The button is dead exactly while a dial is in flight.
  expect({...where, retrying: banners.controls.retrying}).toEqual({
    ...where,
    retrying: controls === 'lost-retrying',
  });
}
