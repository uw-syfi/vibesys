/**
 * Which run notifications a host's new listing calls for: a run that finished, failed or paused
 * since the listing before it. The first listing of a host only sets the baseline, so opening the
 * app never notifies about history.
 */
import {type HostListing, type HostRun, hostFromKey, hostLabel} from '@vibesys/hosts';
import type {RunRef} from '@vibesys/runs';
import {endedStatus, endReason} from './run-status.js';

export type NoticeKind = 'finished' | 'failed' | 'paused';

export interface RunNotice {
  readonly kind: NoticeKind;
  readonly run: RunRef;
  readonly title: string;
  readonly body: string;
}

const TITLES: Readonly<Record<NoticeKind, string>> = {
  finished: 'Run finished',
  failed: 'Run failed',
  paused: 'Run paused',
};

function notice(kind: NoticeKind, listing: HostListing, server: HostRun): RunNotice {
  const project = server.projectRoot.split('/').filter(Boolean).at(-1) ?? server.projectRoot;
  const why = endReason(server);
  const reason = kind === 'failed' && why !== null ? `: ${why}` : '';
  return {
    kind,
    run: {host: listing.host, runId: server.runId},
    title: TITLES[kind],
    body: `${project} on ${hostLabel(hostFromKey(listing.host))}${reason}`,
  };
}

function endedKind(server: HostRun): NoticeKind | null {
  const status = endedStatus(server);
  return status === 'finished' || status === 'failed' ? status : null;
}

/** The notices `after` calls for, given the host's listing `before` it (none for a first one). */
export function runNotices(before: HostListing | undefined, after: HostListing): RunNotice[] {
  if (before?.kind !== 'listed' || after.kind !== 'listed' || before.host !== after.host) return [];
  const wasLive = new Map(before.live.map(server => [server.id, server]));
  const wasEnded = new Set(before.ended.map(server => server.id));
  const ended = after.ended.flatMap(server => {
    const kind = endedKind(server);
    return kind !== null && wasLive.has(server.id) && !wasEnded.has(server.id)
      ? [notice(kind, after, server)]
      : [];
  });
  const paused = after.live.flatMap(server => {
    const previous = wasLive.get(server.id);
    return server.paused !== null && previous !== undefined && previous.paused === null
      ? [notice('paused', after, server)]
      : [];
  });
  return [...ended, ...paused];
}
