import {describe, expect, test} from 'bun:test';
import {BackendClientError} from '@vibesys/backend-client';
import {type CoreRunStatus, hasRunEnded, initialCoreState} from '@vibesys/core-state';
import {connectionBanners} from './banners.js';
import type {WebSessionState, WebSessionStatus} from './session.js';

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

const LOST =
  'Controls lost their connection to the run. Its state cannot be refreshed until they reconnect.';
const COLD = 'Controls have not reached the run. Its state cannot be refreshed until they connect.';

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

describe('connectionBanners', () => {
  test('names losing a connection and never having one as the different failures they are', () => {
    expect(connectionBanners(runWith('running'), sessionState('connected', 'lost'))).toEqual({
      stream: false,
      reattach: false,
      controls: {message: LOST, retrying: false},
    });
    expect(connectionBanners(runWith('running'), sessionState('connected', 'cold'))).toEqual({
      stream: false,
      reattach: false,
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
      stream: false,
      reattach: false,
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

  test('keeps the stream banner on an ended run but drops its reattach button', () => {
    expect(connectionBanners(runWith('completed'), sessionState('stale', 'up'))).toEqual({
      stream: true,
      reattach: false,
      controls: null,
    });
    expect(connectionBanners(runWith('running'), sessionState('stale', 'up'))).toEqual({
      stream: true,
      reattach: true,
      controls: null,
    });
  });

  test('reports the two failures independently', () => {
    expect(connectionBanners(runWith('running'), sessionState('stale', 'lost'))).toEqual({
      stream: true,
      reattach: true,
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

  // A banner appears exactly when the failure it names is being reported, and
  // for the controls only while the run can still act on a reconnect.
  expect({...where, stream: banners.stream}).toEqual({
    ...where,
    stream: sessionStatus === 'stale',
  });
  expect({...where, shown: banners.controls !== null}).toEqual({...where, shown: down && !ended});
  // Every affordance sits inside a banner that is on screen, and none is
  // offered for a run that cannot act on it.
  expect({...where, orphaned: banners.reattach && !banners.stream}).toEqual({
    ...where,
    orphaned: false,
  });
  expect({
    ...where,
    offeredOnEnded: ended && (banners.reattach || banners.controls !== null),
  }).toEqual({...where, offeredOnEnded: false});
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
