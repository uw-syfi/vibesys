/** Run digests, read through the gateway's registered reads.

A digest says at a glance what a run's journal holds (the sidebar's rows and the dashboard's tiles).
It comes from the host's one reader of a project's runs, so it depends on no helper connection. */
import {type Timestamp, timestampMs} from '@bufbuild/protobuf/wkt';
import type {RunAttention, RunClosing} from '@vibesys/contracts/host-helper';
import type {
  HostId,
  RunDashboardEntry,
  RunDigestEntry,
  RunDigestsReply,
  RunEnd,
} from '@vibesys/hosts';
import {
  acceptListRunDigests,
  callListRunDigests,
  IdeaVerdict,
  type ListRunDigestsResponse,
  RunAttention as WireAttention,
  RunClosing as WireClosing,
  type RunDashboard as WireDashboard,
  type RunDigest as WireDigest,
  type RunEnding as WireEnding,
  RunEndOutcome as WireOutcome,
} from '@vibesys/proto';
import {newRequestId} from '@vibesys/request-identity';
import {type RegistryReadPorts, registryTransport, requireCheckout} from './registry-reads.js';

export interface RegistryRunReads {
  /** The digest of each of `runIds` the project has a journal of, with the tiles' data. */
  digests(
    host: HostId,
    project: string,
    runIds: readonly string[],
    signal: AbortSignal,
  ): Promise<RunDigestsReply>;
}

type RunDigestOutcome = NonNullable<RunEnd>['outcome'];

const OUTCOMES: ReadonlyMap<WireOutcome, RunDigestOutcome> = new Map([
  [WireOutcome.ADOPTED, 'adopted'],
  [WireOutcome.KEPT_INPUT, 'kept-input'],
  [WireOutcome.NO_WINNER, 'no-winner'],
  [WireOutcome.ADOPTION_FAILED, 'adoption-failed'],
  [WireOutcome.STOPPED, 'stopped'],
  [WireOutcome.DEADLINE, 'deadline'],
  [WireOutcome.FINISH_REFUSED, 'finish-refused'],
  [WireOutcome.FAILED, 'failed'],
  [WireOutcome.FORCE_STOPPED, 'force_stopped'],
]);

const CLOSINGS: ReadonlyMap<WireClosing, RunClosing> = new Map([
  [WireClosing.FINISHED, 'finished'],
  [WireClosing.FAILED, 'failed'],
  [WireClosing.INTERRUPTED, 'interrupted'],
]);

const VERDICTS: ReadonlyMap<IdeaVerdict, 'kept' | 'not_better' | 'failed' | 'running'> = new Map([
  [IdeaVerdict.KEPT, 'kept'],
  [IdeaVerdict.NOT_BETTER, 'not_better'],
  [IdeaVerdict.FAILED, 'failed'],
  [IdeaVerdict.RUNNING, 'running'],
]);

const ATTENTIONS: ReadonlyMap<WireAttention, RunAttention> = new Map([
  [WireAttention.NONE, 'none'],
  [WireAttention.FAILED, 'failed'],
  [WireAttention.REPEATED_FAILURE, 'repeated_failure'],
  [WireAttention.STALLED, 'stalled'],
]);

/** The member `table` has for `key`, or a refusal that names the unknown value. */
function known<Key, Value>(table: ReadonlyMap<Key, Value>, key: Key, what: string): Value {
  const found = table.get(key);
  if (found === undefined) throw new Error(`the host sent an unknown ${what} (${String(key)})`);
  return found;
}

const ms = (stamp: Timestamp | undefined): number | null =>
  stamp === undefined ? null : timestampMs(stamp);

/** What a run's tile draws; null when the host sent no tile (it was not asked, or has none). */
function tileOf(tile: WireDashboard | undefined): RunDashboardEntry | null {
  if (tile === undefined) return null;
  return {
    points: tile.points.map(({ratio, verdict, at}) => ({
      ratio: ratio ?? null,
      verdict: known(VERDICTS, verdict, 'verdict'),
      at: ms(at),
    })),
    pointsOmitted: tile.pointsOmitted,
    failedStreak: tile.failedStreak,
    lastEventAt: ms(tile.lastEventAt),
    attention: known(ATTENTIONS, tile.attention, 'attention'),
    best:
      tile.best === undefined
        ? null
        : {value: tile.best.value, metric: tile.best.metric ?? null, unit: tile.best.unit ?? null},
  };
}

/** The outcome of a run's ending record; null when it recorded no result. */
function outcomeOf(ending: WireEnding | undefined): RunDigestOutcome | null {
  if (ending === undefined || ending.outcome === WireOutcome.UNSPECIFIED) return null;
  return known(OUTCOMES, ending.outcome, 'ending');
}

function digestOf(createdAt: number, digest: WireDigest): RunDigestEntry {
  const {ending} = digest;
  const closing =
    digest.closing === WireClosing.UNSPECIFIED ? null : known(CLOSINGS, digest.closing, 'closing');
  const summary = ending?.summary ?? '';
  return {
    kind: 'read',
    createdAt,
    ideas: digest.ideas ?? null,
    adoptedRatio: digest.adoptedRatio ?? null,
    bestSoFarRatio: digest.bestSoFarRatio ?? null,
    durationSeconds: digest.durationS ?? null,
    outcome: outcomeOf(ending),
    uncompared: ending?.uncompared === true,
    closing,
    over: ending !== undefined || closing !== null,
    endSummary: summary.trim() === '' ? null : summary,
    dashboard: tileOf(digest.dashboard),
  };
}

/** The entries of a digests response, by run id. */
export function digestEntries(response: ListRunDigestsResponse): Map<string, RunDigestEntry> {
  const entries = new Map<string, RunDigestEntry>();
  for (const {runId, createdAt, result} of response.digests) {
    const created = ms(createdAt) ?? 0;
    if (result.case === 'digest') entries.set(runId, digestOf(created, result.value));
    else if (result.case === 'unreadable')
      entries.set(runId, {kind: 'unreadable', createdAt: created, message: result.value});
  }
  return entries;
}

export function registryRunReads(ports: RegistryReadPorts): RegistryRunReads {
  async function transport(host: HostId) {
    const checkout = requireCheckout(await ports.checkout(host), null);
    return registryTransport(checkout, (bytes, owned) => ports.unary(host, bytes, owned));
  }
  return {
    async digests(host, project, runIds, signal) {
      const response = await callListRunDigests(
        await transport(host),
        acceptListRunDigests({
          requestId: newRequestId(),
          project,
          runIds: [...runIds],
          withDashboard: true,
        }),
        signal,
      );
      return {kind: 'digests', runs: digestEntries(response)};
    },
  };
}
