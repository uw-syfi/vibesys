/**
 * The worlds of the Machines page: This Mac with clusters and a Modal token, a machine with nothing
 * set up, one whose VibeSys predates compute settings, and one that cannot be reached; the cluster
 * tests answer by the cluster's ssh alias. Each machine's runs are the runs its listing names, as
 * the dashboard counts them. Plain seeds with nothing of a browser in it, for the journeys and the
 * DOM tests of the shell.
 */

import type {MachineDescription} from '@vibesys/hosts';
import {computeSettings, computeSettingsEmpty, sshCluster} from '@vibesys/run-fixtures';
import type {FakeBridgeSeed} from './fake-bridge.js';
import {PROJECT, SEED, server} from './seed.js';
import type {HostFeed, RunsFeed, SubscriptionSeed} from './subscription-preload.js';

/** Facts of a machine with GPUs, two runs live. */
const FACTS: MachineDescription = {
  checkout: {root: '/home/me/vibesys', version: '0.1.1', commit: 'e66d941', dirty: false},
  hardware: {
    cpu_cores: 64,
    gpus: Array.from({length: 8}, () => ({vendor: 'nvidia', model: 'H100'})),
    accelerator: 'cuda',
  },
  docker: {reachable: true, version: '29.1'},
  harness_clis: ['claude'],
  harness_cli_names: {claude: 'Claude Code'},
  runs: {live: 2, ended_today: 1},
};

const LOCAL = {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'};
const HOST_A = {host: 'ssh:host-a', label: 'host-a', checkout: '/data/vibesys'};
const HOST_B = {host: 'ssh:host-b', label: 'host-b', checkout: '~/vibesys'};
const HOST_C = {host: 'ssh:host-c', label: 'host-c', checkout: '~/vibesys'};

const NO_LOGINS = null;

/** One world of the page: its machines, how each answers, and the runs on each. */
export interface MachinesWorld {
  readonly name: string;
  readonly seed: FakeBridgeSeed;
  /**
   * What each host's live stats and runs do, over the defaults every world gets: a host streams
   * what its check found, and says the runs its listing holds.
   */
  readonly live?: Partial<SubscriptionSeed>;
  /** A cluster's Test pressed before the page is looked at, and the word it then shows. */
  readonly testFirst?: {
    readonly machine: string;
    readonly cluster: string;
    readonly shows: 'Unreachable since' | 'Testing…';
  };
  /** The cluster tests answer only when the test says (`finishTests`), so one is seen under way. */
  readonly holdTests?: boolean;
}

const runId = (at: number): string => `2026101${at % 10}-1000${String(at).padStart(2, '0')}`;

/**
 * The Fake's listings and project runs for `runs`: each machine lists that many runs, of which
 * `running` are live and the rest ended.
 */
function listing(
  runs: Readonly<Record<string, number>>,
  running: Readonly<Record<string, number>>,
): Pick<FakeBridgeSeed, 'listings' | 'projectRuns'> {
  const hosts = Object.entries(runs).filter(([, count]) => count > 0);
  const live = (host: string) => running[host] ?? 0;
  return {
    listings: hosts.map(([host, count]) => ({
      kind: 'listed',
      host,
      live: Array.from({length: live(host)}, (_, at) =>
        server(`00000000000${at}`, runId(at), 'running'),
      ),
      ended: Array.from({length: count - live(host)}, (_, at) =>
        server(`00000000000${live(host) + at}`, runId(live(host) + at), 'failed'),
      ),
      unreadable: [],
      unverified: [],
    })),
    projectRuns: Object.fromEntries(
      hosts.map(([host, count]) => [
        `${host}|${PROJECT}`,
        Array.from({length: count}, (_, at) => ({
          runId: runId(at),
          task: 'mpmc',
          createdAt: 1_760_000_000_000 - at * 60_000,
        })),
      ]),
    ),
  };
}

/**
 * What a host streams when its world says nothing: its readings if its check passed, and no
 * answer yet if the check found it unreachable, signed out or too old. A host that is reached
 * again (signed in, its checkout set) streams.
 */
function feedOf(report: NonNullable<FakeBridgeSeed['reports']>[string] | undefined): HostFeed {
  return report === undefined || report.kind === 'ready'
    ? {kind: 'live'}
    : {kind: 'connecting', step: 'handshake', attempt: 0};
}

/** A listing the Fake bridge holds, as far as the runs a host says are read from it. */
interface Listed {
  readonly host: string;
  readonly live: readonly unknown[];
  readonly ended: readonly unknown[];
}

const isListed = (value: unknown): value is Listed =>
  typeof value === 'object' &&
  value !== null &&
  'host' in value &&
  'live' in value &&
  Array.isArray(value.live) &&
  'ended' in value &&
  Array.isArray(value.ended);

/** The runs each machine says it holds: those its listing names, alive or ended. */
function runsSaid(seed: FakeBridgeSeed): Record<string, RunsFeed> {
  const said = new Map<string, RunsFeed>();
  for (const listed of seed.listings.filter(isListed))
    said.set(listed.host, {alive: listed.live.length, ended: listed.ended.length});
  return Object.fromEntries(
    (seed.machines ?? []).map(({host}) => [host, said.get(host) ?? {alive: 0, ended: 0}]),
  );
}

function seeded(
  machines: readonly (typeof LOCAL)[],
  settings: NonNullable<FakeBridgeSeed['machineSettings']>,
  runs: Readonly<Record<string, number>>,
  reports: NonNullable<FakeBridgeSeed['reports']> = {},
  running: Readonly<Record<string, number>> = {},
): FakeBridgeSeed {
  return {
    ...SEED,
    recent: [],
    ...listing(runs, running),
    facts: FACTS,
    machines,
    reports,
    machineSettings: settings,
  };
}

const FULL = {compute: computeSettings, logins: NO_LOGINS};
const EMPTY = {compute: computeSettingsEmpty, logins: NO_LOGINS};
const OLD = {compute: null, logins: NO_LOGINS};
const HELD = {
  compute: {
    ...computeSettings,
    clusters: [{cluster: sshCluster, source: 'vibesys'}],
    modal: {token_present: false},
  },
  logins: NO_LOGINS,
};

/** A cluster whose test finds it unreachable: its ssh alias is not one the world names. */
const UNREACHABLE_CLUSTER = {
  compute: {
    ...computeSettings,
    clusters: [
      ...computeSettings.clusters,
      {
        cluster: {...sshCluster, name: 'standby', reach: {kind: 'ssh', host: 'standby'}},
        source: 'vibesys',
      },
    ],
  },
  logins: NO_LOGINS,
};

const GIB = 2 ** 30;
const BUSY = {
  cpuPercent: 34,
  memory: {usedBytes: 18 * GIB, totalBytes: 36 * GIB},
};
const GPUS = {
  cpuPercent: 71,
  memory: {usedBytes: 212 * GIB, totalBytes: 512 * GIB},
  gpus: Array.from({length: 8}, (_, index) => ({
    name: 'H100',
    utilizationPercent: 40 + index * 8,
    usedBytes: 52 * GIB,
    totalBytes: 80 * GIB,
  })),
};

export const WORLDS: readonly MachinesWorld[] = [
  {name: 'one machine', seed: seeded([LOCAL], {local: FULL}, {local: 3})},
  {
    name: 'several machines',
    seed: seeded(
      [LOCAL, HOST_A, HOST_B, HOST_C],
      {local: FULL, 'ssh:host-a': HELD, 'ssh:host-b': EMPTY, 'ssh:host-c': OLD},
      {local: 3, 'ssh:host-a': 1, 'ssh:host-c': 12},
      {},
      {local: 2},
    ),
  },
  {
    name: 'an unreachable machine',
    seed: seeded(
      [LOCAL, HOST_A, HOST_B],
      {local: FULL, 'ssh:host-a': HELD, 'ssh:host-b': EMPTY},
      {local: 1, 'ssh:host-a': 2},
      {
        'ssh:host-a': {kind: 'unavailable', detail: 'the host did not answer in time'},
        'ssh:host-b': {kind: 'sign-in', detail: 'your SSH key needs its passphrase.'},
      },
    ),
  },
  {
    name: 'an unreachable cluster',
    seed: seeded([LOCAL, HOST_A], {local: UNREACHABLE_CLUSTER, 'ssh:host-a': HELD}, {local: 1}),
    testFirst: {machine: 'This Mac', cluster: 'standby', shows: 'Unreachable since'},
  },
  {
    name: 'a live machine',
    seed: seeded([LOCAL], {local: FULL}, {local: 3}),
    live: {hosts: {local: {kind: 'live', readings: BUSY}}},
  },
  {
    name: 'a busy machine',
    seed: seeded([LOCAL], {local: FULL}, {local: 3}, {}, {local: 2}),
    live: {hosts: {local: {kind: 'live', readings: BUSY}}},
  },
  {
    // The machine's check timed out on a loaded host, while the host streams its readings.
    name: 'a loaded machine',
    seed: seeded(
      [LOCAL],
      {local: FULL},
      {local: 3},
      {local: {kind: 'unavailable', detail: 'the host did not answer in time'}},
      {local: 2},
    ),
    live: {hosts: {local: {kind: 'live', readings: BUSY}}},
  },
  {
    name: 'a stale machine',
    seed: seeded([LOCAL], {local: FULL}, {local: 3}),
    live: {hosts: {local: {kind: 'stale', readings: BUSY, reason: 'silent', ageMs: 42_000}}},
  },
  {
    name: 'a connecting machine',
    seed: seeded([LOCAL], {local: FULL}, {local: 3}),
    live: {hosts: {local: {kind: 'connecting', step: 'handshake', attempt: 0}}},
  },
  {
    name: 'a reconnecting machine',
    seed: seeded([LOCAL], {local: FULL}, {local: 3}),
    live: {
      hosts: {
        local: {kind: 'stale', readings: BUSY, reason: 'ended', ageMs: 95_000, attempt: 0},
      },
    },
  },
  {
    name: 'a failed machine',
    seed: seeded([LOCAL], {local: FULL}, {local: 3}),
    live: {
      hosts: {local: {kind: 'failed', reason: 'unreachable', retrying: true}},
      afterRetry: {
        local: {kind: 'stale', readings: BUSY, reason: 'ended', ageMs: 95_000, attempt: 0},
      },
    },
  },
  {
    name: 'a closed machine',
    seed: seeded([LOCAL], {local: FULL}, {local: 3}),
    live: {hosts: {local: {kind: 'failed', reason: 'closed', retrying: false}}},
  },
  {
    name: 'a machine with unread gauges',
    seed: seeded([LOCAL], {local: FULL}, {local: 3}),
    live: {hosts: {local: {kind: 'live', readings: {cpuPercent: null, memory: null, gpus: null}}}},
  },
  {
    name: 'an older machine',
    seed: seeded(
      [LOCAL, HOST_C],
      {local: FULL, 'ssh:host-c': OLD},
      {local: 3},
      {'ssh:host-c': {kind: 'needs-update', facts: null}},
    ),
    live: {
      hosts: {
        local: {kind: 'live', readings: BUSY},
        'ssh:host-c': {kind: 'failed', reason: 'unsupported', retrying: false},
      },
    },
  },
  {
    name: 'two live machines',
    seed: seeded([LOCAL, HOST_A], {local: FULL, 'ssh:host-a': HELD}, {local: 3, 'ssh:host-a': 1}),
    live: {
      hosts: {local: {kind: 'live', readings: BUSY}, 'ssh:host-a': {kind: 'live', readings: GPUS}},
    },
  },
  {
    name: 'four machines in every state',
    seed: seeded(
      [LOCAL, HOST_A, HOST_B, HOST_C],
      {local: FULL, 'ssh:host-a': HELD, 'ssh:host-b': EMPTY, 'ssh:host-c': OLD},
      {local: 3, 'ssh:host-a': 1, 'ssh:host-c': 12},
    ),
    live: {
      hosts: {
        local: {kind: 'live', readings: BUSY},
        'ssh:host-a': {kind: 'stale', readings: GPUS, reason: 'outdated', ageMs: 95_000},
        'ssh:host-b': {kind: 'failed', reason: 'unreachable', retrying: true},
        'ssh:host-c': {kind: 'connecting', step: 'handshake', attempt: 0},
      },
    },
  },
  {
    name: 'a machine with none',
    seed: seeded([LOCAL, HOST_B], {local: FULL, 'ssh:host-b': EMPTY}, {}),
  },
  {
    name: 'a test in progress',
    seed: seeded([LOCAL], {local: FULL}, {local: 2}),
    testFirst: {machine: 'This Mac', cluster: 'lab-h100', shows: 'Testing…'},
    holdTests: true,
  },
];

/** What the hosts stream in `world`: each host what its check found, over what the world scripts. */
export function subscriptionSeedOf(world: MachinesWorld): SubscriptionSeed {
  const {seed} = world;
  return {
    hosts: {
      ...Object.fromEntries(
        (seed.machines ?? []).map(({host}) => [host, feedOf(seed.reports?.[host])]),
      ),
      ...world.live?.hosts,
    },
    runs: {...runsSaid(seed), ...world.live?.runs},
    ...(world.live?.afterRetry === undefined ? {} : {afterRetry: world.live.afterRetry}),
  };
}
