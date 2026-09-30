import {describe, expect, test} from 'bun:test';
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
const outage = new Error('Server disconnected');

function sessionState(status: WebSessionStatus, controlsDown: boolean): WebSessionState {
  return {
    status,
    error: status === 'stale' ? outage : null,
    controls: controlsDown ? {status: 'disconnected', error: outage} : {status: 'connected'},
  };
}

describe('connectionBanners', () => {
  test('shows the controls banner for an undeliverable channel on a live run', () => {
    expect(connectionBanners(runWith('running'), sessionState('connected', true))).toEqual({
      stream: false,
      reattach: false,
      controls: {status: 'disconnected', error: outage},
    });
  });

  test('withholds the controls banner once the run has ended', () => {
    expect(connectionBanners(runWith('completed'), sessionState('connected', true))).toEqual({
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
    const down = sessionState('connected', true);
    expect(connectionBanners(runWith('running'), down).controls).not.toBeNull();
    expect(connectionBanners(runWith('completed'), down).controls).toBeNull();
  });

  test('keeps the stream banner on an ended run but drops its reattach button', () => {
    expect(connectionBanners(runWith('completed'), sessionState('stale', false))).toEqual({
      stream: true,
      reattach: false,
      controls: null,
    });
    expect(connectionBanners(runWith('running'), sessionState('stale', false))).toEqual({
      stream: true,
      reattach: true,
      controls: null,
    });
  });

  test('reports the two failures independently', () => {
    const both = connectionBanners(runWith('running'), sessionState('stale', true));
    expect(both).toEqual({
      stream: true,
      reattach: true,
      controls: {status: 'disconnected', error: outage},
    });
  });

  /**
   * Exhaustive over the closed input space (10 run statuses x 3 session
   * statuses x channel up/down), so the properties hold for every combination
   * rather than the five named above.
   */
  test('holds its properties for every run, stream, and channel combination', () => {
    for (const runStatus of RUN_STATUSES) {
      for (const sessionStatus of SESSION_STATUSES) {
        for (const controlsDown of [false, true]) {
          checkCombination(runStatus, sessionStatus, controlsDown);
        }
      }
    }
  });
});

/** The properties one point of the input space must satisfy. */
function checkCombination(
  runStatus: CoreRunStatus,
  sessionStatus: WebSessionStatus,
  controlsDown: boolean,
): void {
  const run = runWith(runStatus);
  const ended = hasRunEnded(run);
  const banners = connectionBanners(run, sessionState(sessionStatus, controlsDown));
  const where = {runStatus, sessionStatus, controlsDown};

  // A banner appears exactly when the failure it names is being reported, and
  // for the controls only while the run can still act on a reconnect.
  expect({...where, stream: banners.stream}).toEqual({
    ...where,
    stream: sessionStatus === 'stale',
  });
  expect({...where, controls: banners.controls !== null}).toEqual({
    ...where,
    controls: controlsDown && !ended,
  });
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
  // The banner carries the outage the session reported, unchanged.
  expect({...where, carried: banners.controls?.error ?? null}).toEqual({
    ...where,
    carried: controlsDown && !ended ? outage : null,
  });
}
