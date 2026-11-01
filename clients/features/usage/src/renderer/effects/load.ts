/**
 * The questions the Usage page asks over the welcome bridge: what each connected machine made of
 * its usage in the period's buckets, a fresh price list from the machines whose list is due, and
 * the prices a person saved. A machine that cannot be reached, or whose VibeSys predates the
 * question, is counted, not hidden, and does not stop the others being read.
 */
import type {HostKey} from '@vibesys/hosts';
import {
  attempt,
  type UnexpectedFailure,
  type WelcomeBridge,
  type WelcomeResult,
} from '@vibesys/ipc';
import {knownProjects} from '@vibesys/runs';
import {
  changedBook,
  checkRate,
  type HostReply,
  type LimitsAnswer,
  type ModelName,
  type PeriodEdges,
  type ReadAnswer,
  type SaveRequest,
  type SaveResult,
} from '../../state/index.js';

/** The bridge calls the page makes. */
export type UsageBridge = Pick<
  WelcomeBridge,
  | 'overview'
  | 'usageSummary'
  | 'usageLimits'
  | 'usageRatesRefresh'
  | 'usagePricesSave'
  | 'harnesses'
  | 'onListing'
>;

/** The most projects a question names (the host's bound). */
const PROJECTS_MAX = 64;

function value<T>(result: WelcomeResult<T> | UnexpectedFailure): T | null {
  return result.ok ? result.value : null;
}

/** What the machine's catalogue calls its harnesses and models; none when it offers no catalogue. */
async function namesOnce(bridge: UsageBridge, host: HostKey): Promise<readonly ModelName[]> {
  const listing = value(await attempt(() => bridge.harnesses(host)));
  if (listing === null || listing.kind !== 'listed') return [];
  return listing.harnesses.map(({provider, model, display}) => ({
    harness: provider,
    model,
    harnessName: display?.providerName ?? null,
    modelName: display?.modelName ?? null,
  }));
}

/** The catalogue, asked a second time when the first answer was none: a machine just up may not have it yet. */
async function namesOn(bridge: UsageBridge, host: HostKey): Promise<readonly ModelName[]> {
  const first = await namesOnce(bridge, host);
  return first.length > 0 ? first : namesOnce(bridge, host);
}

type Asked =
  | {readonly kind: 'reply'; readonly reply: HostReply}
  | {readonly kind: 'unreachable'}
  | {readonly kind: 'unsupported'};

async function askHost(
  bridge: UsageBridge,
  host: HostKey,
  projects: readonly string[],
  edges: PeriodEdges,
  now: number,
): Promise<Asked> {
  const outcome = value(await attempt(() => bridge.usageSummary(host, projects, edges.edges, now)));
  if (outcome === null) return {kind: 'unreachable'};
  if (outcome.kind === 'unsupported') return {kind: 'unsupported'};
  return {kind: 'reply', reply: {host, projects, summary: outcome.summary}};
}

/**
 * What every connected machine made of its usage in `edges`, except those switched off. Rejects,
 * naming why, when the app cannot even list its machines or nothing could be read; `signal` stops
 * asking once the question is no longer wanted.
 */
export async function readUsage(
  bridge: UsageBridge,
  edges: PeriodEdges,
  now: number,
  hostsOff: readonly HostKey[],
  signal: AbortSignal,
): Promise<ReadAnswer> {
  const overview = await attempt(() => bridge.overview());
  if (!overview.ok) throw new Error(overview.error);
  signal.throwIfAborted();
  const {listings, recent} = overview.value;
  const wanted = listings.filter(listing => !hostsOff.includes(listing.host));
  const listed = wanted.filter(listing => listing.kind === 'listed');
  // A machine that is still connecting has not failed: it is waited for, not counted as down.
  const connecting = wanted.flatMap(listing =>
    listing.kind === 'unavailable' &&
    (listing.connection.kind === 'connecting' || listing.connection.kind === 'reconnecting')
      ? [listing.host]
      : [],
  );
  const down = wanted.flatMap(listing =>
    listing.kind !== 'listed' && !connecting.includes(listing.host) ? [listing.host] : [],
  );
  const known = listed.map(listing => knownProjects(listing, recent, listing.host));
  const [answers, names] = await Promise.all([
    Promise.all(
      listed.map((listing, at) => {
        const all = known[at] ?? [];
        // More projects than a question can name: ask for none, and the host reads every run it
        // indexed. Only runs from before that index are then left out, which the page says.
        return askHost(bridge, listing.host, all.length > PROJECTS_MAX ? [] : all, edges, now);
      }),
    ),
    Promise.all(listed.map(listing => namesOn(bridge, listing.host))),
  ]);
  signal.throwIfAborted();
  const unreachable = [
    ...down,
    ...listed.flatMap((listing, at) => (answers[at]?.kind === 'unreachable' ? [listing.host] : [])),
  ];
  const unsupported = listed.flatMap((listing, at) =>
    answers[at]?.kind === 'unsupported' ? [listing.host] : [],
  );
  const replies = answers.flatMap(answer => (answer.kind === 'reply' ? [answer.reply] : []));
  if (
    replies.length === 0 &&
    unsupported.length === 0 &&
    unreachable.length > 0 &&
    connecting.length === 0
  ) {
    throw new Error('nothing could be read');
  }
  return {
    replies,
    unreachable,
    unsupported,
    connecting,
    names: names.flat(),
    olderRunsUnread: known.some(each => each.length > PROJECTS_MAX),
  };
}

/**
 * Each connected machine's plan limits, except those switched off; a stored reading younger than
 * `maxAgeS` seconds is what a host answers as it is. Rejects, naming why, when the app cannot list
 * its machines or none could be read; `signal` stops asking once the question is no longer wanted.
 */
export async function readLimits(
  bridge: UsageBridge,
  maxAgeS: number,
  now: number,
  hostsOff: readonly HostKey[],
  signal: AbortSignal,
): Promise<LimitsAnswer> {
  const overview = await attempt(() => bridge.overview());
  if (!overview.ok) throw new Error(overview.error);
  signal.throwIfAborted();
  const wanted = overview.value.listings.filter(listing => !hostsOff.includes(listing.host));
  const listed = wanted.filter(listing => listing.kind === 'listed');
  // A machine that is still connecting has not failed: it is waited for, not counted as down.
  const connecting = wanted.flatMap(listing =>
    listing.kind === 'unavailable' &&
    (listing.connection.kind === 'connecting' || listing.connection.kind === 'reconnecting')
      ? [listing.host]
      : [],
  );
  const down = wanted.flatMap(listing =>
    listing.kind !== 'listed' && !connecting.includes(listing.host) ? [listing.host] : [],
  );
  const asked = await Promise.all(
    listed.map(async listing => ({
      host: listing.host,
      outcome: value(await attempt(() => bridge.usageLimits(listing.host, maxAgeS, now))),
    })),
  );
  signal.throwIfAborted();
  const replies = asked.flatMap(({host, outcome}) =>
    outcome?.kind === 'limits' ? [{host, limits: outcome.limits}] : [],
  );
  const unreachable = [
    ...down,
    ...asked.flatMap(({host, outcome}) => (outcome === null ? [host] : [])),
  ];
  const unsupported = asked.flatMap(({host, outcome}) =>
    outcome?.kind === 'unsupported' ? [host] : [],
  );
  if (
    replies.length === 0 &&
    unsupported.length === 0 &&
    unreachable.length > 0 &&
    connecting.length === 0
  ) {
    throw new Error('nothing could be read');
  }
  return {replies, unreachable, unsupported, connecting};
}

/** Ask each of `hosts` to fetch its price list; a failure leaves the page working on the old one. */
export async function refreshRates(
  bridge: UsageBridge,
  hosts: readonly HostKey[],
  now: number,
  signal: AbortSignal,
): Promise<null> {
  signal.throwIfAborted();
  await Promise.all(hosts.map(host => attempt(() => bridge.usageRatesRefresh(host, false, now))));
  return null;
}

/**
 * Save the draft's change to each destination's own book, independently: one host failing, or
 * having moved since the page read it, never hides another's result.
 */
export async function savePrices(
  bridge: UsageBridge,
  request: SaveRequest,
  signal: AbortSignal,
): Promise<readonly SaveResult[]> {
  signal.throwIfAborted();
  const checked = checkRate(request.draft.rate);
  const rate = request.draft.automatic || !checked.ok ? null : checked.rate;
  return Promise.all(
    request.destinations.map(async ({host, book}): Promise<SaveResult> => {
      const made = changedBook(book, request.draft, rate);
      if (!made.ok) return {host, outcome: 'failed', reason: made.reason};
      const result = await attempt(() =>
        bridge.usagePricesSave(host, book.revision, made.book.overrides, made.book.aliases),
      );
      if (!result.ok) return {host, outcome: 'failed', reason: result.error};
      const outcome = result.value;
      if (outcome.kind === 'unsupported') return {host, outcome: 'unsupported'};
      return {host, outcome: outcome.kind, prices: outcome.prices};
    }),
  );
}
