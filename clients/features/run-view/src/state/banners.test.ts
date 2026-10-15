import {describe, expect, test} from 'bun:test';
import {BackendClientError, type RunEvent} from '@vibesys/contracts';
import {runStep, type WebSessionState, type WebSessionStatus} from '@vibesys/run-session';
import {
  type CoreRunStatus,
  hasRunEnded,
  initialCoreState,
  type RunStartupState,
} from '@vibesys/run-state';
import {
  CONTROLS_BANNER_COPY,
  connectionBanners,
  EMPTY_TRANSCRIPT_COPY,
  emptyTranscriptCopy,
  LIVE_SESSION_HEADER_COPY,
  PAGE_CONNECTION_BANNER_COPY,
  pageHeader,
  STARTUP_QUIET_AFTER_MS,
  STARTUP_STAGE_LABELS,
  STREAM_BANNER_COPY,
  startupStatusLabel,
} from './banners.js';
import {formatElapsed} from './duration.js';

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
const {waiting: WAITING, unavailable: UNAVAILABLE, complete: COMPLETE} = EMPTY_TRANSCRIPT_COPY;

function sessionState(
  status: WebSessionStatus,
  controls: 'up' | 'lost' | 'lost-retrying' | 'cold',
  hasSnapshot = true,
): WebSessionState {
  return {
    hasSnapshot,
    status,
    error: status === 'stale' ? outage : null,
    iterationError: null,
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
/**
 * A mid-build progress tick, as the backend's `_StartupReporter` emits it: before
 * `run_started`, which the environment it reports on precedes (#resources.py).
 */
const STARTUP_PROGRESS: RunEvent = {
  protocol_version: 1,
  sequence: 1,
  run_id: 'run-1',
  timestamp: '2026-09-12T10:14:58Z',
  type: 'run_startup_progress',
  text: '',
  status: 'active',
  data: {
    kind: 'run_startup_progress',
    stage: 'building_image',
    progress: {done: 12, total: 30, label: 'RUN pip install'},
  },
};

/** Any instant; headers that are not about startup ignore it. */
const NOW = Date.parse('2026-09-12T10:20:00Z');

describe('connectionBanners', () => {
  test('names a cold unreachable gateway whichever session channel reports first', () => {
    const run = initialCoreState();
    const controlFirst = sessionState('connecting', 'cold', false);
    const streamFirst = sessionState('stale', 'up', false);

    for (const session of [controlFirst, streamFirst]) {
      expect(connectionBanners(run, session).page).toEqual({
        message: PAGE_CONNECTION_BANNER_COPY.cold,
      });
      expect(pageHeader(run, session, NOW)).toEqual({
        heading: LIVE_SESSION_HEADER_COPY.unreachable.heading,
        status: {
          className: 'status-unreachable',
          label: LIVE_SESSION_HEADER_COPY.unreachable.status,
        },
      });
    }

    const beforeEitherFailure = sessionState('connecting', 'up', false);
    expect(connectionBanners(run, beforeEitherFailure).page).toBeNull();
    expect(pageHeader(run, beforeEitherFailure, NOW)).toEqual({
      heading: LIVE_SESSION_HEADER_COPY.connecting.heading,
      status: {
        className: 'status-connecting',
        label: LIVE_SESSION_HEADER_COPY.connecting.status,
      },
    });
  });

  test('keeps the control-channel diagnostic separate from the page diagnostic', () => {
    const run = initialCoreState();
    const cold = connectionBanners(run, sessionState('connecting', 'cold', false));
    const pageRecovered = connectionBanners(run, sessionState('connected', 'cold', true));

    expect(cold.page).not.toBeNull();
    expect(pageRecovered.page).toBeNull();
    expect(cold.controls).toEqual({message: COLD, retrying: false});
    expect(pageRecovered.controls).toEqual(cold.controls);
  });

  test('reports a lost gateway only after both established channels are down', () => {
    const run = runWith('running');
    expect(connectionBanners(run, sessionState('stale', 'up', true)).page).toBeNull();
    expect(connectionBanners(run, sessionState('connected', 'lost', true)).page).toBeNull();
    expect(connectionBanners(run, sessionState('stale', 'lost', true)).page).toEqual({
      message: PAGE_CONNECTION_BANNER_COPY.lost,
    });
    expect(connectionBanners(run, sessionState('stale', 'cold', true)).page).toEqual({
      message: PAGE_CONNECTION_BANNER_COPY.lost,
    });
  });

  test('reserves replay loading copy for replay-driven pages', () => {
    const run = initialCoreState();
    expect(pageHeader(run, null, NOW).heading).toBe('Replay is loading');
    expect(pageHeader(run, sessionState('connected', 'up', true), NOW).heading).toBe(
      'Run overview',
    );
  });

  test('takes the banner down on the run-ending event itself, folded by the real reducer', () => {
    // The property below builds its `CoreState` by assigning `status`, so it
    // asserts that the decision is right about a status without asserting that
    // any real input produces that status. This closes that: the run's own
    // terminal event goes through `@vibesys/run-state`'s reducer, which is the path the
    // browser takes, and is the same event `e2e/controls-banner.spec.ts`
    // withholds and then releases.
    let run = initialCoreState();
    const lostChannel = sessionState('connected', 'lost');

    run = runStep(run, {type: 'events', events: [RUN_STARTED]}).state;
    expect(hasRunEnded(run)).toBe(false);
    expect(connectionBanners(run, lostChannel).controls).toEqual({
      message: LOST,
      retrying: false,
    });

    run = runStep(run, {type: 'events', events: [RUN_FINISHED]}).state;
    // Both facts come off the same folded state, which is why a render cannot
    // show one without the other: the status the header reads and the predicate
    // the banner reads are the same field of the same value.
    expect(run.status).toBe('completed');
    expect(hasRunEnded(run)).toBe(true);
    expect(connectionBanners(run, lostChannel).controls).toBeNull();

    // The stream banner crosses the same transition the other way: the gap it
    // names outlives the run, and the explicit reload remains available.
    const staleStream = sessionState('stale', 'up');
    expect(connectionBanners(run, staleStream).stream).toEqual({
      message: GAP_FINAL,
      reattach: true,
    });
  });

  test('names losing a connection and never having one as the different failures they are', () => {
    expect(connectionBanners(runWith('running'), sessionState('connected', 'lost'))).toEqual({
      page: null,
      stream: null,
      controls: {message: LOST, retrying: false},
    });
    expect(connectionBanners(runWith('running'), sessionState('connected', 'cold'))).toEqual({
      page: null,
      stream: null,
      controls: {message: COLD, retrying: false},
    });
  });

  test('names no operation this client cannot perform', () => {
    // `query.snapshot` is the only request `clients/apps/web` issues, so promising
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
      page: null,
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
   * wrong once the run is over, so the copy says so and `Reattach` offers the
   * session's explicit fresh-bootstrap recovery. A command path nobody can
   * reach stops costing the reader anything, so it goes silent.
   */
  test('keeps the stream banner and reload affordance on an ended run', () => {
    expect(connectionBanners(runWith('completed'), sessionState('stale', 'up'))).toEqual({
      page: null,
      stream: {message: GAP_FINAL, reattach: true},
      controls: null,
    });
    expect(connectionBanners(runWith('running'), sessionState('stale', 'up'))).toEqual({
      page: null,
      stream: {message: GAP_OPEN, reattach: true},
      controls: null,
    });
    // The same ended run, with the control channel down instead of the stream:
    // the opposite verdict, from the same `hasRunEnded`.
    expect(connectionBanners(runWith('completed'), sessionState('stale', 'lost'))).toEqual({
      page: {message: PAGE_CONNECTION_BANNER_COPY.lost},
      stream: {message: GAP_FINAL, reattach: true},
      controls: null,
    });
  });

  test('reports the two failures independently', () => {
    expect(connectionBanners(runWith('running'), sessionState('stale', 'lost'))).toEqual({
      page: {message: PAGE_CONNECTION_BANNER_COPY.lost},
      stream: {message: GAP_OPEN, reattach: true},
      controls: {message: LOST, retrying: false},
    });
  });

  test('pairs an empty transcript with its stale stream banner', () => {
    const run = runWith('completed');
    const banners = connectionBanners(run, sessionState('stale', 'up'));

    expect({banner: banners.stream?.message, empty: emptyTranscriptCopy(run, banners)}).toEqual({
      banner: GAP_FINAL,
      empty: UNAVAILABLE,
    });
    expect(emptyTranscriptCopy(run, banners)).not.toContain('Waiting');
    expect(emptyTranscriptCopy(run, banners)).not.toContain('replay');
  });

  /**
   * Exhaustive over the closed input space (10 run statuses x 3 session
   * statuses x 4 channel states x 2 snapshot states), so the properties hold
   * for every combination rather than the handful named above.
   */
  test('holds its properties for every run, stream, channel, and snapshot combination', () => {
    for (const runStatus of RUN_STATUSES) {
      for (const sessionStatus of SESSION_STATUSES) {
        for (const controls of CHANNEL_STATES) {
          for (const hasSnapshot of [false, true]) {
            checkCombination(runStatus, sessionStatus, controls, hasSnapshot);
          }
        }
      }
    }
  });
});

describe('the status chip while a run is starting', () => {
  const connected = sessionState('connected', 'up');

  test('narrates the stage and progress of the environment opening, worded like the Host Panel', () => {
    const run = runStep(runWith('starting'), {type: 'events', events: [STARTUP_PROGRESS]}).state;
    expect(pageHeader(run, connected, STARTUP_AT + 5_000).status).toEqual({
      className: 'status-starting',
      label: `${STARTUP_STAGE_LABELS.building_image} 12/30: RUN pip install · 5s`,
    });
  });

  test('keeps the bare status word while starting has reported no progress yet', () => {
    expect(pageHeader(runWith('starting'), connected, NOW).status).toEqual({
      className: 'status-starting',
      label: 'starting',
    });
  });

  test('stops narrating startup progress once the run is ready, though the fact is retained', () => {
    let run = runStep(runWith('starting'), {type: 'events', events: [STARTUP_PROGRESS]}).state;
    run = runStep(run, {type: 'events', events: [{...RUN_STARTED, sequence: 2}]}).state;

    expect(run.status).toBe('running');
    // The last startup fact survives the transition undisturbed (`QuotaState.pause`'s
    // discipline): nothing clears it, because nothing has to. Showing it is `pageHeader`'s
    // call, made from `status` alone, which is why the chip reverts to the bare word.
    expect(run.startup).toEqual({
      stage: 'building_image',
      progress: {done: 12, total: 30, label: 'RUN pip install'},
      stageSince: STARTUP_PROGRESS.timestamp,
      lastActivityAt: STARTUP_PROGRESS.timestamp,
    });
    expect(pageHeader(run, connected, NOW).status).toEqual({
      className: 'status-running',
      label: 'running',
    });
  });
});

const STARTUP_AT = Date.parse(STARTUP_PROGRESS.timestamp);

function startupWith(
  stage: RunStartupState['stage'],
  progress: RunStartupState['progress'],
  stageSinceMs: number,
  lastActivityMs: number,
): RunStartupState {
  return {
    stage,
    progress,
    stageSince: new Date(stageSinceMs).toISOString(),
    lastActivityAt: new Date(lastActivityMs).toISOString(),
  };
}

describe('startupStatusLabel', () => {
  const T0 = Date.parse('2026-09-12T10:00:00Z');
  const SECOND = 1000;
  const BUILD_PROGRESS = {done: 7, total: 8, label: 'WORKDIR'};

  test('renders the stage, its progress and the time in the stage', () => {
    const table: ReadonlyArray<
      readonly [RunStartupState['stage'], RunStartupState['progress'], number, string]
    > = [
      ['resolving', null, 0, 'Resolving · 0s'],
      ['resolving', null, 45 * SECOND, 'Resolving · 45s'],
      ['building_image', BUILD_PROGRESS, 252 * SECOND, 'Building image 7/8: WORKDIR · 4m12s'],
      ['building_image', {done: 1, total: null, label: ''}, 3 * SECOND, 'Building image 1/? · 3s'],
      ['starting_container', null, 3783 * SECOND, 'Starting container · 1h03m'],
    ];
    for (const [stage, progress, elapsed, expected] of table) {
      // Heard from just now, so only the stage clock is under test.
      const startup = startupWith(stage, progress, T0, T0 + elapsed);
      expect(startupStatusLabel(startup, T0 + elapsed)).toBe(expected);
    }
  });

  test('says nothing is being heard only once the silence reaches the threshold', () => {
    const startup = startupWith('building_image', BUILD_PROGRESS, T0, T0 + 10 * SECOND);
    const quietFrom = T0 + 10 * SECOND + STARTUP_QUIET_AFTER_MS;
    for (const [now, quiet] of [
      [T0 + 10 * SECOND, false],
      [quietFrom - 1, false],
      [quietFrom, true],
      [quietFrom + 60 * SECOND, true],
    ] as const) {
      const label = startupStatusLabel(startup, now);
      expect({now: now - T0, quiet: label.includes('quiet for')}).toEqual({now: now - T0, quiet});
    }
    expect(startupStatusLabel(startup, quietFrom + 60 * SECOND)).toBe(
      'Building image 7/8: WORKDIR · 2m10s · quiet for 2m00s',
    );
  });

  test('clamps a clock earlier than the timestamps to zero', () => {
    const startup = startupWith('resolving', null, T0, T0);
    expect(startupStatusLabel(startup, T0 - 90 * SECOND)).toBe('Resolving · 0s');
  });

  test('drops the timing it cannot parse rather than printing it', () => {
    const startup = {...startupWith('resolving', null, T0, T0), stageSince: 'not a time'};
    expect(startupStatusLabel(startup, T0 + 5 * SECOND)).toBe('Resolving');
  });
});

describe('formatElapsed', () => {
  test('uses seconds, minutes with seconds, then hours with minutes', () => {
    const table: ReadonlyArray<readonly [number, string]> = [
      [-5000, '0s'],
      [0, '0s'],
      [999, '0s'],
      [45_000, '45s'],
      [59_999, '59s'],
      [60_000, '1m00s'],
      [252_000, '4m12s'],
      [3_599_000, '59m59s'],
      [3_600_000, '1h00m'],
      [3_783_000, '1h03m'],
      [Number.NaN, '0s'],
    ];
    for (const [ms, expected] of table) expect(formatElapsed(ms)).toBe(expected);
  });
});

const CHANNEL_STATES = ['up', 'lost', 'lost-retrying', 'cold'] as const;

/** The properties one point of the input space must satisfy. */
function checkCombination(
  runStatus: CoreRunStatus,
  sessionStatus: WebSessionStatus,
  controls: (typeof CHANNEL_STATES)[number],
  hasSnapshot: boolean,
): void {
  const run = runWith(runStatus);
  const ended = hasRunEnded(run);
  const session = sessionState(sessionStatus, controls, hasSnapshot);
  const banners = connectionBanners(run, session);
  const header = pageHeader(run, session, NOW);
  const empty = emptyTranscriptCopy(run, banners);
  const where = {runStatus, sessionStatus, controls, hasSnapshot};
  const down = controls !== 'up';
  checkPagePresentation(where, banners, header);

  // A banner appears exactly when the failure it names is being reported.
  // Whether the stream's appears is independent of the run's status (only what
  // it says below depends on it): a transcript that stopped short is short
  // whatever status sits above it, and on an ended run this banner is the only
  // thing on the page that says so.
  expect({...where, shown: banners.stream !== null}).toEqual({
    ...where,
    shown: sessionStatus === 'stale',
  });
  // The empty panel is a projection of the same facts as the banner. In
  // particular, a stream fault wins over terminal status because the fold may
  // be empty only because its bootstrap batch did not arrive.
  expect({...where, empty}).toEqual({
    ...where,
    empty: sessionStatus === 'stale' ? UNAVAILABLE : ended ? COMPLETE : WAITING,
  });
  expect({...where, callsLiveGatewayAReplay: empty.includes('replay')}).toEqual({
    ...where,
    callsLiveGatewayAReplay: false,
  });
  // The controls' does, and only while the run can still act on a reconnect.
  expect({...where, shown: banners.controls !== null}).toEqual({...where, shown: down && !ended});
  // Stream recovery follows observed stream state rather than run status: a
  // stale ended transcript can be reloaded, while controls remain irrelevant.
  expect({...where, reattach: banners.stream?.reattach ?? false}).toEqual({
    ...where,
    reattach: sessionStatus === 'stale',
  });
  expect({...where, endedControls: ended && banners.controls !== null}).toEqual({
    ...where,
    endedControls: false,
  });
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
    expect({...where, reattach: banners.stream.reattach}).toEqual({...where, reattach: true});
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

type Combination = Readonly<{
  runStatus: CoreRunStatus;
  sessionStatus: WebSessionStatus;
  controls: (typeof CHANNEL_STATES)[number];
  hasSnapshot: boolean;
}>;

function checkPagePresentation(
  where: Combination,
  banners: ReturnType<typeof connectionBanners>,
  header: ReturnType<typeof pageHeader>,
): void {
  const controlsDown = where.controls !== 'up';
  const gatewayDown = where.hasSnapshot
    ? where.sessionStatus === 'stale' && controlsDown
    : where.sessionStatus === 'stale' || controlsDown;
  const gatewayFailure =
    where.hasSnapshot || where.controls === 'lost' || where.controls === 'lost-retrying'
      ? 'lost'
      : 'cold';
  expect({...where, shown: banners.page !== null}).toEqual({...where, shown: gatewayDown});
  expect({...where, message: banners.page?.message ?? null}).toEqual({
    ...where,
    message: gatewayDown ? PAGE_CONNECTION_BANNER_COPY[gatewayFailure] : null,
  });
  const connectionHeader = gatewayDown
    ? LIVE_SESSION_HEADER_COPY.unreachable
    : LIVE_SESSION_HEADER_COPY.connecting;
  expect({...where, heading: header.heading}).toEqual({
    ...where,
    heading: where.hasSnapshot ? 'Run overview' : connectionHeader.heading,
  });
  expect({...where, chip: header.status.label}).toEqual({
    ...where,
    chip: where.hasSnapshot ? where.runStatus : connectionHeader.status,
  });
}
