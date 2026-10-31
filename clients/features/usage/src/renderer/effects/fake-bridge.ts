/**
 * A Fake of the bridge calls the Usage question makes, for tests: machines with projects with runs,
 * and what each project's helper answers for a window. It records every question it was asked.
 */
import type {
  HostKey,
  HostListing,
  ListedRun,
  UsageWindowEntry,
  UsageWindowOutcome,
} from '@vibesys/hosts';
import type {HarnessListing, WelcomeEvent, WelcomeResult} from '@vibesys/ipc';
import type {RecentRun} from '@vibesys/runs';
import type {UsageBridge} from './load.js';

export const ZERO_USAGE: UsageWindowEntry = {
  since: 0,
  until: 0,
  spentUsd: 0,
  spendPerHourUsd: 0,
  tokens: {input: null, cache_read: null, cache_write: null, output: null, reasoning: null},
  turns: 0,
  unpricedTurns: 0,
  unpricedItemised: true,
  untimedTurns: 0,
  newBests: 0,
  costPerNewBestUsd: null,
  byRun: [],
  byAgent: [],
  agentsOmitted: 0,
  missingRuns: [],
};

export interface FakeProject {
  readonly host: HostKey;
  readonly project: string;
  /** Newest first. `null`: the machine cannot list the project's runs. */
  readonly runs: readonly ListedRun[] | null;
  /** What the helper answers; `null`: the question fails. */
  readonly answer: UsageWindowOutcome | null;
}

export interface FakeWorld {
  readonly projects: readonly FakeProject[];
  /** Machines the app cannot reach. */
  readonly unavailable?: readonly HostKey[];
  readonly harnesses?: Readonly<Record<string, HarnessListing>>;
  readonly overviewFails?: boolean;
}

const ok = <T>(value: T): WelcomeResult<T> => ({ok: true, value});
const failed = <T>(error: string): WelcomeResult<T> => ({ok: false, error, authNeeded: false});

const listedHost = (host: HostKey): HostListing => ({
  kind: 'listed',
  host,
  live: [],
  ended: [],
  unreadable: [],
  unverified: [],
});

const unavailableHost = (host: HostKey): HostListing => ({
  kind: 'unavailable',
  host,
  step: 'gateway',
  connection: {kind: 'offline', detail: 'unreachable'},
});

export class FakeUsageBridge implements UsageBridge {
  /** Every usage question: where it went, the runs it named and its window. */
  readonly asked: {
    readonly host: HostKey;
    readonly project: string;
    readonly runIds: readonly string[];
    readonly since: number;
    readonly until: number;
  }[] = [];
  readonly overviews: number[] = [];
  /** The machines that cannot be reached now; they answer nothing until `connect`. */
  readonly #down: Set<HostKey>;
  readonly #listeners: ((event: WelcomeEvent<HostListing>) => void)[] = [];
  #sequence = 0;
  /** Held until released: the questions of a world whose answers the test settles itself. */
  #hold: Promise<void> | null = null;

  constructor(readonly world: FakeWorld) {
    this.#down = new Set(world.unavailable ?? []);
  }

  /** The machine's connection comes up: it answers from now on, and its listing is pushed. */
  connect(host: HostKey): void {
    this.#down.delete(host);
    this.#push(listedHost(host));
  }

  /** The machine's connection goes down: nothing is read from it, and its listing is pushed. */
  disconnect(host: HostKey): void {
    this.#down.add(host);
    this.#push(unavailableHost(host));
  }

  #push(listing: HostListing): void {
    this.#sequence += 1;
    for (const listener of this.#listeners) listener({sequence: this.#sequence, value: listing});
  }

  onListing: UsageBridge['onListing'] = listener => {
    this.#listeners.push(listener);
  };

  /** Hold every usage answer until `release` is called. */
  holdAnswers(): () => void {
    let release = (): void => undefined;
    this.#hold = new Promise<void>(resolve => {
      release = resolve;
    });
    return release;
  }

  overview: UsageBridge['overview'] = async () => {
    this.overviews.push(this.overviews.length);
    if (this.world.overviewFails === true) return failed('The machines could not be listed');
    const hosts = [...new Set(this.world.projects.map(each => each.host))];
    const listed = hosts.filter(host => !this.#down.has(host)).map(listedHost);
    const unavailable = [...this.#down].map(unavailableHost);
    // The projects are known from the runs the app opened in them.
    const recent: RecentRun[] = this.world.projects.map(each => ({
      host: each.host,
      hostLabel: each.host,
      project: each.project,
      task: null,
      runId: 'seen',
      instanceId: null,
      attachedAt: 1,
    }));
    return ok({eventCursor: 0, recent, skippedRecent: [], listings: [...listed, ...unavailable]});
  };

  projectRuns: UsageBridge['projectRuns'] = async (host, project) => {
    const found = this.world.projects.find(each => each.host === host && each.project === project);
    if (this.#down.has(host)) return failed('The connection is down');
    if (found === undefined || found.runs === null) return failed('Could not list the runs');
    return ok(found.runs);
  };

  usageWindow: UsageBridge['usageWindow'] = async (host, project, runIds, since, until) => {
    this.asked.push({host, project, runIds, since, until});
    await this.#hold;
    if (this.#down.has(host)) return failed('The connection is down');
    const found = this.world.projects.find(each => each.host === host && each.project === project);
    if (found?.answer == null) return failed('Could not read what the runs spent');
    return ok(found.answer);
  };

  harnesses: UsageBridge['harnesses'] = async host =>
    ok(this.world.harnesses?.[host] ?? {kind: 'unsupported' as const});
}
