/**
 * The world a run fixture is opened in: the listing that shows its run live or ended, the machines
 * it is on, what the user opened. A plain seed for the Fake bridge, with nothing of a browser in
 * it, so the journeys (`e2e/fixture-world.ts`) and the DOM tests of the shell open the same world.
 */
import {
  clusterTestsByAlias,
  type EndOutcome,
  loginStartsByProvider,
  machineSettingsWorld,
  type RunFixture,
} from '@vibesys/run-fixtures';
import {type FakeBridgeSeed, MAC_CHROME} from './fake-bridge.js';
import {FAKE_OFFER} from './fake-offer.js';
import {STARTED_AT} from './started-at.js';

const PROJECT = '/work/queue-rs';
const TASK = 'mpmc';
const INSTANCE = '00000000000a';
const HOST = 'ssh:remote-host';
const CHECKOUT = '~/vibesys';

/** The registry phase a run that ended with `outcome` stays in. */
function phaseOf(outcome: EndOutcome | undefined): string {
  if (outcome === undefined) return 'running';
  if (outcome === 'failed' || outcome === 'adoption-failed') return 'failed';
  if (outcome === 'stopped' || outcome === 'force_stopped') return 'stopped';
  return 'completed';
}

function server(fixture: RunFixture): Record<string, unknown> {
  const end = fixture.end;
  const phase = phaseOf(end?.outcome);
  return {
    id: INSTANCE,
    projectRoot: PROJECT,
    runId: fixture.runId,
    status: phase,
    phase,
    paused: null,
    end: end === undefined ? null : {...end, resumable: false},
    startedAt: STARTED_AT,
    reason: null,
  };
}

/** A listing of the fixture's run, live or ended as the fixture says. */
function listingOf(fixture: RunFixture): Record<string, unknown> {
  const run = server(fixture);
  const live = fixture.state === 'live';
  return {
    kind: 'listed',
    host: HOST,
    live: live ? [run] : [],
    ended: live ? [] : [run],
    unreadable: [],
    unverified: [],
  };
}

/**
 * The runs `host` lists for `PROJECT`: those its servers and registry hold, and those the user
 * opened (Recent), as `runs.list` would answer. The one place the world says which runs a machine
 * has, so the dashboard and the machines agree.
 */
function projectRuns(
  listing: Record<string, unknown>,
  recent: readonly Record<string, unknown>[],
): Record<string, readonly unknown[]> {
  const servers = [...(listing['live'] as unknown[]), ...(listing['ended'] as unknown[])];
  const runs = new Map<string, unknown>();
  for (const each of servers) {
    const {runId, startedAt} = each as {runId: string; startedAt: number};
    runs.set(runId, {runId, task: TASK, createdAt: startedAt * 1000});
  }
  for (const each of recent) {
    const {runId, task, attachedAt} = each as {runId: string; task: string; attachedAt: number};
    if (!runs.has(runId)) runs.set(runId, {runId, task, createdAt: attachedAt});
  }
  return {[`${HOST}|${PROJECT}`]: [...runs.values()]};
}

export function seedOf(fixture: RunFixture, theme: 'ink' | 'paper'): FakeBridgeSeed {
  const listing = listingOf(fixture);
  const recent = [
    {
      host: HOST,
      hostLabel: 'remote-host',
      project: PROJECT,
      task: TASK,
      runId: fixture.runId,
      instanceId: fixture.state === 'live' ? INSTANCE : null,
      attachedAt: 3,
    },
  ];
  return {
    theme,
    chromeInset: MAC_CHROME,
    appearance: {mode: theme === 'ink' ? 'dark' : 'light', light: 'paper', dark: 'ink'},
    listings: [listing],
    recent,
    // The machines are the ones the run is on: This Mac, and its host with the run's checkout.
    machines: [
      {host: 'local', label: 'This Mac', checkout: CHECKOUT},
      {host: HOST, label: 'remote-host', checkout: CHECKOUT},
    ],
    projectRuns: projectRuns(listing, recent),
    newRun: {...FAKE_OFFER, projects: {[PROJECT]: [TASK]}},
    machineSettings: machineSettingsWorld,
    clusterTests: clusterTestsByAlias,
    loginStarts: loginStartsByProvider,
  };
}
