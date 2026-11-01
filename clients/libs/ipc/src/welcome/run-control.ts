/**
 * The run-control feature's welcome requests: attach, resume, stop (the view asks the user first),
 * letting a run go, and reading a run's recorded history and its summary.
 */

import type {UsageRecord} from '@vibesys/contracts/host-helper';
import {
  type HostId,
  type HostKey,
  hostFromKey,
  type ListedRun,
  type RunDigestsReply,
  type UsageAliasEntry,
  type UsageLimitsOutcome,
  type UsagePricesOutcome,
  type UsageRateEntry,
  type UsageRateOverrideEntry,
  type UsageRatesOutcome,
  type UsageSummaryOutcome,
  usageSummaryRefusal,
} from '@vibesys/hosts';
import {exactResumeRunId, type ResumeRunId} from '@vibesys/runs';
import type {WelcomeResult} from './types.js';
import {type ChannelOf, instance, operationToken, project} from './values.js';

export const RUN_CONTROL_CHANNELS = {
  attach: 'vibesys:welcome:attach',
  attachReader: 'vibesys:welcome:attach-reader',
  resume: 'vibesys:welcome:resume',
  releaseRun: 'vibesys:welcome:release-run',
  readEvents: 'vibesys:welcome:read-events',
  readTrace: 'vibesys:welcome:read-trace',
  readSummary: 'vibesys:welcome:read-summary',
  readSpan: 'vibesys:welcome:read-span',
  runDigests: 'vibesys:welcome:run-digests',
  projectRuns: 'vibesys:welcome:project-runs',
  cancelProjectRuns: 'vibesys:welcome:cancel-project-runs',
  usageSummary: 'vibesys:welcome:usage-summary',
  usageRatesRefresh: 'vibesys:welcome:usage-rates-refresh',
  usageLimits: 'vibesys:welcome:usage-limits',
  usagePricesSave: 'vibesys:welcome:usage-prices-save',
} as const;

/** The most runs one digest request may name (the helper's bound). */
const RUN_DIGESTS_MAX = 100;

/** The most events one history page may ask for. */
export const HISTORY_PAGE_MAX = 2000;

/**
 * One page of a run's recorded events (`runs.read_events`, feature `run_history`): the events from
 * the asked sequence on, as the helper recorded them (the page validates each before folding it),
 * where the next page starts, and whether the history is complete; or that this host's VibeSys
 * does not serve history.
 */
export type RunHistoryPage =
  | {
      readonly kind: 'page';
      readonly events: readonly unknown[];
      readonly next: number;
      readonly complete: boolean;
      /**
       * The journal's events and bytes when the page was read, for progress; absent from a host
       * that does not name them (`run_history_pages`). A live run's journal grows past them.
       */
      readonly totalEvents?: number;
      readonly totalBytes?: number;
    }
  | {readonly kind: 'unsupported'};

/** A count of tokens spent as a host's summary words it; `null`: not reported. */
export interface SummaryTokens {
  readonly input: number | null;
  readonly cacheRead: number | null;
  readonly cacheWrite: number | null;
  readonly output: number | null;
  readonly reasoning: number | null;
}

/**
 * A run's summary (`runs.summary`, feature `run_summary`) with its pages joined: what the run level
 * shows, as the journal stood at `throughSequence`. The documents are the run protocol's, as the
 * helper recorded them (the page validates them before folding them): the spans in the order they
 * started, the overview, and each turn's last typed reply with its schema.
 */
export interface RunSummary {
  readonly throughSequence: number;
  readonly eventCount: number;
  readonly journalBytes: number;
  /**
   * Whether the journal records a run trace. `predates_trace`: `spans` is empty because a VibeSys
   * from before the trace wrote the journal, not because the run tried nothing, and
   * `roundsFinished` is all it says of the run's work.
   */
  readonly trace: 'recorded' | 'predates_trace';
  readonly roundsFinished: number;
  readonly overview: unknown;
  readonly spans: readonly unknown[];
  readonly replies: readonly {
    readonly spanId: string;
    readonly kind: string;
    readonly jsonSchema: unknown;
    readonly value: unknown;
  }[];
  /** The whole run's spend, every turn's. */
  readonly tokens: SummaryTokens;
  /** Each turn span's spend. */
  readonly usage: readonly {readonly spanId: string; readonly tokens: SummaryTokens}[];
  /** One record per finished turn, as the helper words it: its tokens sum to `tokens`. */
  readonly records: readonly UsageRecord[];
  /** The gates that finished before the first idea began, in journal order. */
  readonly inputGates: readonly SummaryGate[];
  /** How the journal ended, when a terminal event ended it; null while it has not. */
  readonly terminal: SummaryTerminal | null;
}

/** One gate that finished before the run's first idea, as a summary words it. */
interface SummaryGate {
  /** The gate event's journal sequence. */
  readonly sequence: number;
  /** The gate's kind: `build`, `accuracy`, `benchmark` or `validation`. */
  readonly gate: string;
  readonly recipe: string | null;
  readonly passed: boolean;
  /** The end of what the gate printed, credentials redacted; null when it kept nothing. */
  readonly outputTail: string | null;
}

/** The terminal event that ended a run's journal, with its whole message. */
interface SummaryTerminal {
  readonly sequence: number;
  readonly kind: 'finished' | 'failed' | 'interrupted';
  /** The event's whole account of why, not cut to a line; null when it said nothing. */
  readonly message: string | null;
}

/**
 * A run's summary, that this host's VibeSys does not serve one, or that the host holds no record
 * of the run (`unknown_run`): a run the sidebar calls gone.
 */
export type RunSummaryResult =
  | {readonly kind: 'summary'; readonly summary: RunSummary}
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'gone'};

/** One bounded page of a span's recorded, non-trace events. */
export type RunSpanPage =
  | {
      readonly kind: 'page';
      readonly events: readonly unknown[];
      readonly next: number;
      readonly complete: boolean;
      readonly total: number;
    }
  | {readonly kind: 'unsupported'};

/**
 * The window message type that hands the shell page one run connection's port: `connectRun(id)`
 * makes the preload post `{type: RUN_PORT_MESSAGE, id}` to the page with the port as its transfer,
 * because a `MessagePort` cannot cross the context bridge as a value.
 */
export const RUN_PORT_MESSAGE = 'vibesys:run-port';

/**
 * What attaching the server Chat reads a run through came to: it is attached to this window, or
 * this host's VibeSys cannot serve a run that has ended (`chat_ended_runs`).
 */
export type ReaderAttached = {readonly kind: 'attached'} | {readonly kind: 'unsupported'};

/** The welcome bridge's requests the run-control feature makes. */
export interface RunControlWelcomeBridge {
  attach(host: HostKey, instance: string): Promise<WelcomeResult<null>>;
  /**
   * Attach, for Chat, the server that serves run `runId` of `project` on `host`: its live one, or
   * for a run that ended a read-only one the host starts, which ends by itself once this window
   * lets the run go. Chat connects to it as to an attached run (`connectRun`).
   */
  attachReader(
    host: HostKey,
    project: string,
    runId: string,
  ): Promise<WelcomeResult<ReaderAttached>>;
  resume(host: HostKey, project: string, run: ResumeRunId): Promise<WelcomeResult<null>>;
  /**
   * Open one connection to the run the window attached for this view (`attach` binds it), over
   * the same relay a run page uses; the port arrives as a `RUN_PORT_MESSAGE` window message named
   * `id`. A view with no attached run gets a port the main process closes at once.
   */
  connectRun(id: string): void;
  /** The machine woke and the attached run's host is reachable again: redial now. */
  onWake(listener: () => void): void;
  /** Let the attached run go: the view shows no live run now. Safe when none is attached. */
  releaseRun(): Promise<WelcomeResult<null>>;
  /** One page of run `runId`'s recorded events in `project` on `host`, from `fromSequence`. */
  readEvents(
    host: HostKey,
    project: string,
    runId: string,
    fromSequence: number,
    max: number,
  ): Promise<WelcomeResult<RunHistoryPage>>;
  /**
   * One page of run `runId`'s recorded events as `readEvents` reads it, with only the events the
   * run trace folds (its spans, its declaration and its gates) kept: all the Run screen needs to
   * place an ended run's scrubber, and a small part of a journal. The page's `next` and `complete`
   * are the host's whatever it kept, and a page may hold few events or none and not be complete.
   */
  readTrace(
    host: HostKey,
    project: string,
    runId: string,
    fromSequence: number,
    max: number,
  ): Promise<WelcomeResult<RunHistoryPage>>;
  /** Run `runId`'s summary in `project` on `host`: its run level without its history. */
  readSummary(
    host: HostKey,
    project: string,
    runId: string,
  ): Promise<WelcomeResult<RunSummaryResult>>;
  readSpan(
    host: HostKey,
    project: string,
    runId: string,
    spanId: string,
    fromSequence: number,
    max: number,
  ): Promise<WelcomeResult<RunSpanPage>>;
  /**
   * What each of `runIds` in `project` on `host` says at a glance (`runs.list` with `digest_runs`):
   * ideas, best ratio, duration, ending; or that this host's VibeSys does not offer digests.
   */
  runDigests(
    host: HostKey,
    project: string,
    runIds: readonly string[],
  ): Promise<WelcomeResult<RunDigestsReply>>;
  /**
   * Every run of `project` on `host`, newest first, before any digest is read. `token` names the
   * listing (`nextOperationToken`), so `cancelProjectRuns` can end it while it is read.
   */
  projectRuns(
    host: HostKey,
    project: string,
    token: number,
  ): Promise<WelcomeResult<readonly ListedRun[]>>;
  /** End the listing `token` named, which the view no longer wants; one that answered is a no-op. */
  cancelProjectRuns(token: number): Promise<WelcomeResult<null>>;
  /**
   * What `host` made of its usage over the buckets between `edges` (milliseconds, whole quarter
   * hours, increasing) in each of `projects`: figures priced at today's list prices; or that this
   * host's VibeSys does not offer it. `now` is the time the view asks at: the host reads no clock.
   */
  usageSummary(
    host: HostKey,
    projects: readonly string[],
    edges: readonly number[],
    now: number,
  ): Promise<WelcomeResult<UsageSummaryOutcome>>;
  /** Ask `host` to fetch its price list if one is due (`force`: even if the last is under a day old). */
  usageRatesRefresh(
    host: HostKey,
    force: boolean,
    now: number,
  ): Promise<WelcomeResult<UsageRatesOutcome>>;
  /**
   * Ask `host` for each provider's plan limits; a stored reading younger than `maxAgeS` seconds is
   * answered as it is.
   */
  usageLimits(
    host: HostKey,
    maxAgeS: number,
    now: number,
  ): Promise<WelcomeResult<UsageLimitsOutcome>>;
  /**
   * Replace the prices a person set on `host`, edited from the book at `baseRevision`: the book is
   * saved whole, or the host answers with the book that moved.
   */
  usagePricesSave(
    host: HostKey,
    baseRevision: number,
    overrides: readonly UsageRateOverrideEntry[],
    aliases: readonly UsageAliasEntry[],
  ): Promise<WelcomeResult<UsagePricesOutcome>>;
}

export type RunControlRequest =
  | {readonly kind: 'attach'; readonly host: HostId; readonly instance: string}
  | {
      readonly kind: 'attach-reader';
      readonly host: HostId;
      readonly project: string;
      readonly runId: string;
    }
  | {
      readonly kind: 'resume';
      readonly host: HostId;
      readonly project: string;
      /** The exact run to resume. */
      readonly run: ResumeRunId;
    }
  | {readonly kind: 'release-run'}
  | {
      readonly kind: 'read-events';
      readonly host: HostId;
      readonly project: string;
      readonly runId: string;
      readonly from: number;
      readonly max: number;
    }
  | {
      readonly kind: 'read-trace';
      readonly host: HostId;
      readonly project: string;
      readonly runId: string;
      readonly from: number;
      readonly max: number;
    }
  | {
      readonly kind: 'read-summary';
      readonly host: HostId;
      readonly project: string;
      readonly runId: string;
    }
  | {
      readonly kind: 'read-span';
      readonly host: HostId;
      readonly project: string;
      readonly runId: string;
      readonly spanId: string;
      readonly from: number;
      readonly max: number;
    }
  | {
      readonly kind: 'run-digests';
      readonly host: HostId;
      readonly project: string;
      readonly runIds: readonly string[];
    }
  | {
      readonly kind: 'project-runs';
      readonly host: HostId;
      readonly project: string;
      readonly token: number;
    }
  | {readonly kind: 'cancel-project-runs'; readonly token: number}
  | {
      readonly kind: 'usage-summary';
      readonly host: HostId;
      readonly projects: readonly string[];
      readonly edges: readonly number[];
      readonly now: number;
    }
  | {
      readonly kind: 'usage-rates-refresh';
      readonly host: HostId;
      readonly force: boolean;
      readonly now: number;
    }
  | {
      readonly kind: 'usage-limits';
      readonly host: HostId;
      readonly maxAgeS: number;
      readonly now: number;
    }
  | {
      readonly kind: 'usage-prices-save';
      readonly host: HostId;
      readonly baseRevision: number;
      readonly overrides: readonly UsageRateOverrideEntry[];
      readonly aliases: readonly UsageAliasEntry[];
    };

function sequence(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new Error('the first sequence to read must be a non-negative integer');
  }
  return value;
}

function positiveSequence(value: unknown): number {
  const parsed = sequence(value);
  if (parsed === 0) throw new Error('the first span sequence must be positive');
  return parsed;
}

function spanId(value: unknown): string {
  if (typeof value !== 'string' || !/^(flow|op):\S+$/.test(value)) {
    throw new Error('the span id must name a flow or operation span');
  }
  return value;
}

function pageSize(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) {
    throw new Error('the page size must be a positive integer');
  }
  if (value > HISTORY_PAGE_MAX) throw new Error(`the page size is at most ${HISTORY_PAGE_MAX}`);
  return value;
}

function runIds(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > RUN_DIGESTS_MAX) {
    throw new Error(`name at most ${RUN_DIGESTS_MAX} runs`);
  }
  return value.map(each => exactResumeRunId(each));
}

/** The longest a stored limits reading may be served as it is: the host's bound, one day. */
const LIMITS_MAX_AGE_S = 86_400;

function ageSeconds(value: unknown): number {
  if (!Number.isInteger(value) || (value as number) < 0 || (value as number) > LIMITS_MAX_AGE_S) {
    throw new Error(
      'the age a stored limits reading may have must be whole seconds, at most a day',
    );
  }
  return value as number;
}

function instant(value: unknown, which: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`the usage ${which} must be a time in milliseconds`);
  }
  return value;
}

function projectList(value: unknown): string[] {
  if (!Array.isArray(value)) throw new Error('name the projects to read');
  return value.map(each => project(each));
}

function edgeList(value: unknown): number[] {
  if (!Array.isArray(value)) throw new Error('give the edges of the usage buckets');
  return value.map(each => instant(each, 'edge'));
}

function price(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1_000_000) {
    throw new Error(`the ${what} price must be a number of dollars per million tokens`);
  }
  return value;
}

/** At most this many entries and this many bytes a name, as the host's book allows. */
const BOOK_ENTRIES_MAX = 256;
const BOOK_NAME_BYTES_MAX = 128;

/** The bytes `text` takes in UTF-8. */
function utf8Length(text: string): number {
  let bytes = 0;
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    bytes += code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
  }
  return bytes;
}

function bookName(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0 || utf8Length(value) > BOOK_NAME_BYTES_MAX) {
    throw new Error(`a model name is 1 to ${BOOK_NAME_BYTES_MAX} bytes`);
  }
  return value;
}

function objectOf(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null) throw new Error(`${what} must be an object`);
  return value as Record<string, unknown>;
}

function rateEntry(value: unknown): UsageRateEntry {
  const rate = objectOf(value, 'a price');
  const writeOneHour = rate['cacheWrite1h'];
  return {
    input: price(rate['input'], 'input'),
    output: price(rate['output'], 'output'),
    cacheRead: price(rate['cacheRead'], 'cache read'),
    cacheWrite: price(rate['cacheWrite'], 'cache write'),
    cacheWrite1h: writeOneHour === null ? null : price(writeOneHour, 'one hour cache write'),
  };
}

function overrideList(value: unknown): UsageRateOverrideEntry[] {
  if (!Array.isArray(value) || value.length > BOOK_ENTRIES_MAX) {
    throw new Error(`at most ${BOOK_ENTRIES_MAX} prices`);
  }
  return value.map(each => {
    const entry = objectOf(each, 'a price entry');
    return {model: bookName(entry['model']), rate: rateEntry(entry['rate'])};
  });
}

function aliasList(value: unknown): UsageAliasEntry[] {
  if (!Array.isArray(value) || value.length > BOOK_ENTRIES_MAX) {
    throw new Error(`at most ${BOOK_ENTRIES_MAX} Map to entries`);
  }
  return value.map(each => {
    const entry = objectOf(each, 'a Map to entry');
    return {from: bookName(entry['from']), to: bookName(entry['to'])};
  });
}

type UsageChannel =
  | typeof RUN_CONTROL_CHANNELS.usageSummary
  | typeof RUN_CONTROL_CHANNELS.usageRatesRefresh
  | typeof RUN_CONTROL_CHANNELS.usageLimits
  | typeof RUN_CONTROL_CHANNELS.usagePricesSave;

const USAGE_CHANNELS: ReadonlySet<string> = new Set<UsageChannel>([
  RUN_CONTROL_CHANNELS.usageSummary,
  RUN_CONTROL_CHANNELS.usageRatesRefresh,
  RUN_CONTROL_CHANNELS.usageLimits,
  RUN_CONTROL_CHANNELS.usagePricesSave,
]);

function isUsageChannel(channel: string): channel is UsageChannel {
  return USAGE_CHANNELS.has(channel);
}

function usageLimitsRequest(host: unknown, maxAge: unknown, now: unknown): RunControlRequest {
  return {
    kind: 'usage-limits',
    host: hostFromKey(host),
    maxAgeS: ageSeconds(maxAge),
    now: instant(now, 'now'),
  };
}

/** The typed request of a Usage page channel; throws naming the first invalid value. */
function parseUsageRequest(channel: UsageChannel, args: readonly unknown[]): RunControlRequest {
  const [first, second, third, fourth] = args;
  switch (channel) {
    case RUN_CONTROL_CHANNELS.usageSummary: {
      const projects = projectList(second);
      const edges = edgeList(third);
      const now = instant(fourth, 'now');
      const refusal = usageSummaryRefusal(projects, edges);
      if (refusal !== null) throw new Error(refusal);
      return {kind: 'usage-summary', host: hostFromKey(first), projects, edges, now};
    }
    case RUN_CONTROL_CHANNELS.usageRatesRefresh:
      if (typeof second !== 'boolean') throw new Error('say whether to force the refresh');
      return {
        kind: 'usage-rates-refresh',
        host: hostFromKey(first),
        force: second,
        now: instant(third, 'now'),
      };
    case RUN_CONTROL_CHANNELS.usageLimits:
      return usageLimitsRequest(first, second, third);
    case RUN_CONTROL_CHANNELS.usagePricesSave:
      return {
        kind: 'usage-prices-save',
        host: hostFromKey(first),
        baseRevision: sequence(second),
        overrides: overrideList(third),
        aliases: aliasList(fourth),
      };
  }
}

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseRunControlRequest(
  channel: ChannelOf<typeof RUN_CONTROL_CHANNELS>,
  args: readonly unknown[],
): RunControlRequest {
  if (isUsageChannel(channel)) return parseUsageRequest(channel, args);
  const [first, second, third, fourth, fifth, sixth] = args;
  switch (channel) {
    case RUN_CONTROL_CHANNELS.attach:
      return {kind: 'attach', host: hostFromKey(first), instance: instance(second, 'attach to')};
    case RUN_CONTROL_CHANNELS.attachReader:
      return {
        kind: 'attach-reader',
        host: hostFromKey(first),
        project: project(second),
        runId: exactResumeRunId(third),
      };
    case RUN_CONTROL_CHANNELS.resume:
      return {
        kind: 'resume',
        host: hostFromKey(first),
        project: project(second),
        run: exactResumeRunId(third),
      };
    case RUN_CONTROL_CHANNELS.releaseRun:
      return {kind: 'release-run'};
    case RUN_CONTROL_CHANNELS.readEvents:
      return {
        kind: 'read-events',
        host: hostFromKey(first),
        project: project(second),
        runId: exactResumeRunId(third),
        from: sequence(fourth),
        max: pageSize(fifth),
      };
    case RUN_CONTROL_CHANNELS.readTrace:
      return {
        kind: 'read-trace',
        host: hostFromKey(first),
        project: project(second),
        runId: exactResumeRunId(third),
        from: sequence(fourth),
        max: pageSize(fifth),
      };
    case RUN_CONTROL_CHANNELS.readSummary:
      return {
        kind: 'read-summary',
        host: hostFromKey(first),
        project: project(second),
        runId: exactResumeRunId(third),
      };
    case RUN_CONTROL_CHANNELS.readSpan:
      return {
        kind: 'read-span',
        host: hostFromKey(first),
        project: project(second),
        runId: exactResumeRunId(third),
        spanId: spanId(fourth),
        from: positiveSequence(fifth),
        max: pageSize(sixth),
      };
    case RUN_CONTROL_CHANNELS.runDigests:
      return {
        kind: 'run-digests',
        host: hostFromKey(first),
        project: project(second),
        runIds: runIds(third),
      };
    case RUN_CONTROL_CHANNELS.projectRuns:
      return {
        kind: 'project-runs',
        host: hostFromKey(first),
        project: project(second),
        token: operationToken(third, 'project listing'),
      };
    case RUN_CONTROL_CHANNELS.cancelProjectRuns:
      return {kind: 'cancel-project-runs', token: operationToken(first, 'project listing')};
  }
}
