import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {BackendClientError, type RunEvent} from '@vibesys/backend-client';
import {
  type CoreRunStatus,
  hasRunEnded,
  initialCoreState,
  reduceEventBatch,
} from '@vibesys/core-state';
import {CONTROLS_BANNER_COPY, controlsBanner} from './banners.js';
import type {WorkspaceState} from './session.js';

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
const CHANNEL_STATES = ['up', 'lost', 'lost-retrying', 'cold'] as const;
type ChannelState = (typeof CHANNEL_STATES)[number];

const runWith = (status: CoreRunStatus) => ({...initialCoreState(), status});
const outage = new BackendClientError('disconnected', 'Server disconnected');

// Imported rather than retyped. What this file pins is which of the two a given
// state selects, and the properties the text has to hold (below); retyping the
// strings would only pin that someone copied them correctly, and a second copy
// is what let the e2e spec drift onto text the source no longer produced.
const {lost: LOST, cold: COLD} = CONTROLS_BANNER_COPY;

function sessionState(
  core: WorkspaceState['core'],
  controls: ChannelState,
): Pick<WorkspaceState, 'core' | 'controls'> {
  return {
    core,
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

test('takes the banner down on the run-ending event itself, folded by the real reducer', () => {
  // The property below builds its `CoreState` by assigning `status`, so it
  // asserts that the decision is right about a status without asserting that
  // any real input produces that status. This closes that: the run's own
  // terminal event goes through `core-state`'s reducer, which is the path the
  // browser takes, and is the same event `e2e/controls-banner.spec.ts`
  // withholds and then releases.
  const started = reduceEventBatch(initialCoreState(), [RUN_STARTED]);
  assert.equal(hasRunEnded(started), false);
  assert.deepEqual(controlsBanner(sessionState(started, 'lost')), {
    message: LOST,
    retrying: false,
  });

  const finished = reduceEventBatch(started, [RUN_FINISHED]);
  // Both facts come off the same folded state, which is why a render cannot
  // show one without the other: the status the header reads and the predicate
  // the banner reads are the same field of the same value.
  assert.equal(finished.status, 'completed');
  assert.equal(hasRunEnded(finished), true);
  assert.equal(controlsBanner(sessionState(finished, 'lost')), null);
});

test('names losing a connection and never having one as the different failures they are', () => {
  assert.deepEqual(controlsBanner(sessionState(runWith('running'), 'lost')), {
    message: LOST,
    retrying: false,
  });
  assert.deepEqual(controlsBanner(sessionState(runWith('running'), 'cold')), {
    message: COLD,
    retrying: false,
  });
});

test('names no operation this client cannot perform', () => {
  for (const controls of ['lost', 'cold'] as const) {
    const banner = controlsBanner(sessionState(runWith('running'), controls));
    // The page's own words, never the transport's: a "WebSocket transport
    // error" names neither what the user lost nor what to do about it.
    assert.ok(!banner?.message.includes(outage.message));
  }
});

test('marks the reconnect affordance dead while a dial is already in flight', () => {
  assert.deepEqual(controlsBanner(sessionState(runWith('running'), 'lost-retrying')), {
    message: LOST,
    retrying: true,
  });
});

/**
 * The regression for the latched banner: the session keeps reporting the outage
 * it observed, and the decision has to change when the run ends even though the
 * control channel never speaks again. Deciding at report time cannot do this,
 * because the two facts arrive over independent sockets and nothing re-enters
 * the session when the later one lands.
 */
test('takes the controls banner down when the run ends after the outage', () => {
  assert.notEqual(controlsBanner(sessionState(runWith('running'), 'lost')), null);
  assert.equal(controlsBanner(sessionState(runWith('completed'), 'lost')), null);
});

/**
 * Exhaustive over the closed input space (10 run statuses x 4 channel states),
 * so the properties hold for every combination rather than the handful named
 * above.
 */
test('holds its properties for every run and channel combination', () => {
  for (const runStatus of RUN_STATUSES) {
    for (const controls of CHANNEL_STATES) {
      checkCombination(runStatus, controls);
    }
  }
});

/** The properties one point of the input space must satisfy. */
function checkCombination(runStatus: CoreRunStatus, controls: ChannelState): void {
  const core = runWith(runStatus);
  const ended = hasRunEnded(core);
  const banner = controlsBanner(sessionState(core, controls));
  const where = `${runStatus}/${controls}`;

  // A banner appears exactly when the failure it names is being reported, and
  // only while the run can still act on a reconnect.
  assert.equal(banner !== null, controls !== 'up' && !ended, where);
  if (banner === null) return;
  // The copy describes the failure the channel reported, and never shows the
  // transport's own string.
  assert.equal(banner.message, controls === 'cold' ? COLD : LOST, where);
  assert.ok(!banner.message.includes(outage.message), where);
  // The button is dead exactly while a dial is in flight.
  assert.equal(banner.retrying, controls === 'lost-retrying', where);
}
