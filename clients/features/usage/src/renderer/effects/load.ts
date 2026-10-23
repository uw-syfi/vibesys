/**
 * The question the Usage page asks for a window, over the welcome bridge: the projects the app has
 * met on each connected machine, each project's runs, and what those runs spent in the window. A
 * machine that cannot be reached, a project it cannot list, or a helper that predates the question
 * is counted, not hidden, and does not stop the others being read.
 */
import {type HostKey, type HostListing, USAGE_WINDOW_MAX_RUNS} from '@vibesys/hosts';
import {attempt, type WelcomeBridge, type WelcomeResult} from '@vibesys/ipc';
import {knownProjects} from '@vibesys/runs';
import type {ModelName, UsageOutcome, UsageSource, UsageSpan} from '../../state/index.js';

/** The bridge calls the question makes. */
export type UsageBridge = Pick<
  WelcomeBridge,
  'overview' | 'projectRuns' | 'usageWindow' | 'harnesses' | 'onListing'
>;

type Listed = Extract<HostListing, {readonly kind: 'listed'}>;

/** What one project answered, or why it did not. */
type ProjectAnswer =
  | {readonly kind: 'source'; readonly source: UsageSource}
  | {readonly kind: 'none'}
  | {readonly kind: 'unreachable'}
  | {readonly kind: 'unsupported'};

function value<T>(result: WelcomeResult<T> | {readonly ok: false}): T | null {
  return result.ok ? result.value : null;
}

async function askProject(
  bridge: UsageBridge,
  host: HostKey,
  project: string,
  span: UsageSpan,
): Promise<ProjectAnswer> {
  const runs = value(await attempt(() => bridge.projectRuns(host, project)));
  if (runs === null) return {kind: 'unreachable'};
  // The helper answers for at most this many runs, and the listing is newest first.
  const asked = runs.slice(0, USAGE_WINDOW_MAX_RUNS).map(run => run.runId);
  const leftOut = runs.length - asked.length;
  if (asked.length === 0) return {kind: 'none'};
  const outcome = value(
    await attempt(() => bridge.usageWindow(host, project, asked, span.since, span.until)),
  );
  if (outcome === null) return {kind: 'unreachable'};
  if (outcome.kind === 'unsupported') return {kind: 'unsupported'};
  return {kind: 'source', source: {host, project, usage: outcome.usage, leftOut}};
}

/** What the machine's catalogue calls its harnesses and models; none when it offers no catalogue. */
async function namesOn(bridge: UsageBridge, host: HostKey): Promise<readonly ModelName[]> {
  const listing = value(await attempt(() => bridge.harnesses(host)));
  if (listing === null || listing.kind !== 'listed') return [];
  return listing.harnesses.map(({provider, model, display}) => ({
    harness: provider,
    model,
    harnessName: display?.providerName ?? provider,
    modelName: display?.modelName ?? null,
  }));
}

/**
 * What the projects of every connected machine spent in `span`. Rejects, naming why, when the app
 * cannot even list its machines; `signal` stops asking once the question is no longer wanted.
 */
export async function loadUsage(
  bridge: UsageBridge,
  span: UsageSpan,
  signal: AbortSignal,
): Promise<UsageOutcome> {
  const overview = await attempt(() => bridge.overview());
  if (!overview.ok) throw new Error(overview.error);
  signal.throwIfAborted();
  const {listings, recent} = overview.value;
  const listed = listings.filter((listing): listing is Listed => listing.kind === 'listed');
  const asks = listed.flatMap(listing =>
    knownProjects(listing, recent, listing.host).map(project => ({host: listing.host, project})),
  );
  const [answers, names] = await Promise.all([
    Promise.all(asks.map(ask => askProject(bridge, ask.host, ask.project, span))),
    Promise.all(listed.map(listing => namesOn(bridge, listing.host))),
  ]);
  signal.throwIfAborted();
  const sources = answers.flatMap(answer => (answer.kind === 'source' ? [answer.source] : []));
  const unsupported = answers.filter(answer => answer.kind === 'unsupported').length;
  if (sources.length === 0 && unsupported > 0) return {kind: 'unsupported'};
  const unreachable =
    listings.length -
    listed.length +
    answers.filter(answer => answer.kind === 'unreachable').length;
  // Every project asked, or every machine when none was, failed: nothing was read, which is not
  // "no usage".
  const nothingRead =
    answers.length > 0
      ? answers.every(answer => answer.kind === 'unreachable')
      : listed.length === 0 && unreachable > 0;
  if (nothingRead) throw new Error('nothing could be read');
  return {
    kind: 'usage',
    loaded: {
      sources,
      names: names.flat(),
      unreachable,
      unsupported,
    },
  };
}
