/**
 * A Fake of the bridge calls the Usage page makes, for tests: machines that answer a usage question
 * in the buckets it names, keep a price book that saves against its revision as a host does, and
 * can be down, old, or slow. It records every question it was asked.
 */
import type {
  HostKey,
  HostListing,
  UsageAliasEntry,
  UsagePriceBookEntry,
  UsagePricingEntry,
  UsageRateOverrideEntry,
  UsageSeriesEntry,
  UsageSummaryEntry,
} from '@vibesys/hosts';
import type {HarnessListing, WelcomeEvent, WelcomeResult} from '@vibesys/ipc';
import type {RecentRun} from '@vibesys/runs';
import type {UsageBridge} from './load.js';

const FRESH_PRICING: UsagePricingEntry = {
  status: 'fresh',
  fetchedAt: 1_000,
  knownModels: 3,
  refreshDue: false,
  checkedOn: '2026-10-30',
};

export const EMPTY_BOOK: UsagePriceBookEntry = {revision: 0, overrides: [], aliases: []};

/** A series of `columns` over the buckets it is populated in; every column it leaves out is zero. */
export function seriesOf(
  key: Partial<Pick<UsageSeriesEntry, 'origin' | 'provider' | 'model' | 'speed' | 'source'>>,
  buckets: readonly number[],
  columns: Partial<
    Record<
      Exclude<keyof UsageSeriesEntry, keyof typeof key | 'buckets' | 'usageSource'>,
      readonly number[]
    >
  > = {},
): UsageSeriesEntry {
  const zeros = buckets.map(() => 0);
  const column = (name: keyof typeof columns): readonly number[] => columns[name] ?? zeros;
  return {
    origin: key.origin ?? 'run',
    provider: key.provider ?? 'claude',
    model: key.model ?? 'claude-opus-5-5',
    speed: key.speed ?? 'standard',
    source: key.source ?? 'model',
    usageSource: 0,
    buckets,
    cached: column('cached'),
    cacheWrite: column('cacheWrite'),
    uncached: column('uncached'),
    output: column('output'),
    reasoning: column('reasoning'),
    turns: column('turns'),
    approximateTurns: column('approximateTurns'),
    recordedUsd: column('recordedUsd'),
    estimateUsd: column('estimateUsd'),
    inputUsd: column('inputUsd'),
    cacheReadUsd: column('cacheReadUsd'),
    cacheWriteUsd: column('cacheWriteUsd'),
    outputUsd: column('outputUsd'),
    speedPremiumUsd: column('speedPremiumUsd'),
    cacheSavingsUsd: column('cacheSavingsUsd'),
  };
}

/** What a host that has seen nothing answers. */
export function emptySummary(book: UsagePriceBookEntry = EMPTY_BOOK): UsageSummaryEntry {
  return {
    series: [],
    counts: [],
    bests: [],
    runs: [],
    runsOther: [],
    agents: [],
    agentsOther: [],
    recent: [],
    sources: [
      {
        kind: 'journal',
        sourceId: 'journal',
        provider: '',
        root: '',
        status: 'ok',
        scanned: 0,
        skipped: 0,
        malformed: 0,
        failed: 0,
        distinctSessions: 0,
        excludedExact: 0,
        excludedApproximate: 0,
      },
    ],
    pricing: FRESH_PRICING,
    prices: book,
    pricesStatus: 'ok',
    rates: [],
    pendingRuns: 0,
    pendingFiles: 0,
    foldedSeries: 0,
    readAt: 0,
  };
}

/** What a host answers for a question of that many buckets, given the book it holds. */
export type FakeAnswer = (buckets: number, book: UsagePriceBookEntry) => UsageSummaryEntry;

interface FakeHost {
  readonly host: HostKey;
  /** The projects the app has met on the machine. */
  readonly projects?: readonly string[];
  /** What it answers for a question of that many buckets, that it is old, or that it fails. */
  readonly answer: FakeAnswer | 'unsupported' | 'fails';
  readonly book?: UsagePriceBookEntry;
}

export interface FakeWorld {
  readonly hosts: readonly FakeHost[];
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
  /** Every usage question: where it went, what it named and the time it asked at. */
  readonly asked: {
    readonly host: HostKey;
    readonly projects: readonly string[];
    readonly edges: readonly number[];
    readonly now: number;
  }[] = [];
  readonly refreshed: {readonly host: HostKey; readonly force: boolean}[] = [];
  readonly saves: {
    readonly host: HostKey;
    readonly baseRevision: number;
    readonly overrides: readonly UsageRateOverrideEntry[];
    readonly aliases: readonly UsageAliasEntry[];
  }[] = [];
  readonly overviews: number[] = [];
  /** The machines that cannot be reached now; they answer nothing until `connect`. */
  readonly #down: Set<HostKey>;
  /** The book each host holds, and the saves that fail. */
  readonly #books = new Map<HostKey, UsagePriceBookEntry>();
  readonly #saveFails = new Set<HostKey>();
  readonly #listeners: ((event: WelcomeEvent<HostListing>) => void)[] = [];
  #sequence = 0;
  /** Held until released: the questions of a world whose answers the test settles itself. */
  #hold: Promise<void> | null = null;

  constructor(readonly world: FakeWorld) {
    this.#down = new Set(world.unavailable ?? []);
    for (const each of world.hosts) this.#books.set(each.host, each.book ?? EMPTY_BOOK);
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

  /** The book `host` holds now. */
  bookOf(host: HostKey): UsagePriceBookEntry {
    return this.#books.get(host) ?? EMPTY_BOOK;
  }

  /** Save to `host` fails from now on, as a host that is reached but cannot write. */
  failSaves(host: HostKey): void {
    this.#saveFails.add(host);
  }

  /** Another person changes `host`'s book: its revision moves under the page. */
  edit(host: HostKey, book: Omit<UsagePriceBookEntry, 'revision'>): void {
    this.#books.set(host, {...book, revision: this.bookOf(host).revision + 1});
  }

  #push(listing: HostListing): void {
    this.#sequence += 1;
    for (const listener of this.#listeners) listener({sequence: this.#sequence, value: listing});
  }

  onListing: UsageBridge['onListing'] = listener => {
    this.#listeners.push(listener);
  };

  /** Hold every answer until the returned function is called. */
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
    const listed = this.world.hosts.filter(each => !this.#down.has(each.host));
    const unavailable = [...this.#down].map(unavailableHost);
    // The projects are known from the runs the app opened in them.
    const recent: RecentRun[] = this.world.hosts.flatMap(each =>
      (each.projects ?? ['/p']).map(project => ({
        host: each.host,
        hostLabel: each.host,
        project,
        task: null,
        runId: 'seen',
        instanceId: null,
        attachedAt: 1,
      })),
    );
    return ok({
      eventCursor: 0,
      recent,
      skippedRecent: [],
      listings: [...listed.map(each => listedHost(each.host)), ...unavailable],
    });
  };

  usageSummary: UsageBridge['usageSummary'] = async (host, projects, edges, now) => {
    this.asked.push({host, projects, edges, now});
    await this.#hold;
    if (this.#down.has(host)) return failed('The connection is down');
    const found = this.world.hosts.find(each => each.host === host);
    if (found === undefined || found.answer === 'fails') return failed('Could not read the usage');
    if (found.answer === 'unsupported') return ok({kind: 'unsupported' as const});
    const book = this.bookOf(host);
    const summary = found.answer(Math.max(0, edges.length - 1), book);
    return ok({kind: 'summary' as const, summary: {...summary, prices: book}});
  };

  usageRatesRefresh: UsageBridge['usageRatesRefresh'] = async (host, force) => {
    this.refreshed.push({host, force});
    if (this.#down.has(host)) return failed('The connection is down');
    return ok({kind: 'pricing' as const, pricing: FRESH_PRICING});
  };

  /** A host saves a book whole against its revision; the same book saved twice is saved once. */
  usagePricesSave: UsageBridge['usagePricesSave'] = async (
    host,
    baseRevision,
    overrides,
    aliases,
  ) => {
    this.saves.push({host, baseRevision, overrides, aliases});
    await this.#hold;
    if (this.#down.has(host) || this.#saveFails.has(host)) return failed('The connection is down');
    const found = this.world.hosts.find(each => each.host === host);
    if (found?.answer === 'unsupported') return ok({kind: 'unsupported' as const});
    const book = this.bookOf(host);
    if (baseRevision !== book.revision) return ok({kind: 'moved' as const, prices: book});
    const same =
      JSON.stringify(book.overrides) === JSON.stringify(overrides) &&
      JSON.stringify(book.aliases) === JSON.stringify(aliases);
    const saved = same
      ? book
      : {revision: book.revision + 1, overrides: [...overrides], aliases: [...aliases]};
    this.#books.set(host, saved);
    return ok({kind: 'saved' as const, prices: saved});
  };

  harnesses: UsageBridge['harnesses'] = async host =>
    ok(this.world.harnesses?.[host] ?? {kind: 'unsupported' as const});
}
