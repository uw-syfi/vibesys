/**
 * What a registry record says about its run, in the sidebar's words: a live run is running or paused
 * (`paused` names who paused it); an ended one finished, failed or stopped, from its end record's
 * outcome, or from its last phase on a tombstone that records no end.
 */
import {type EndTone, endedLook, type HostListing, type HostRun} from '@vibesys/hosts';

/** What a sidebar row says about its run; never shown by color alone. */
export type RunRowStatus = 'live' | 'paused' | 'finished' | 'failed' | 'stopped' | 'unknown';

/** The status of a live server's run. */
export function liveStatus(server: Omit<HostRun, 'projectRoot'>): RunRowStatus {
  return server.paused === null ? 'live' : 'paused';
}

/** The status of an ended server's run, or `unknown` when there is no record of it. */
export function endedStatus(server: StatusRun | undefined): RunRowStatus {
  return (server === undefined ? null : endedLook(server))?.kind ?? 'unknown';
}

/** What a row's dot and detail say: the tone and the words, never by color alone. */
export interface RowLook {
  readonly tone: EndTone | 'live';
  readonly word: string;
}

/** The look of a live server's run. */
export function liveLook(server: Omit<HostRun, 'projectRoot'>): RowLook {
  return server.paused === null
    ? {tone: 'live', word: statusWord('live')}
    : {tone: 'warn', word: statusWord('paused')};
}

const WORDS: Readonly<Record<RunRowStatus, string>> = {
  live: 'running',
  paused: 'paused',
  finished: 'finished',
  failed: 'failed',
  stopped: 'stopped',
  unknown: "can't read run state",
};

/** The word for a row's status. */
export function statusWord(status: RunRowStatus): string {
  return WORDS[status];
}

/** A live run's detail: its phase in words, or who paused it. */
export function liveDetail(server: Omit<HostRun, 'projectRoot'>): string {
  if (server.paused === 'quota') return 'paused: usage limit';
  if (server.paused === 'user') return 'paused by you';
  return server.status;
}

/** Why a run ended, in one line, when its record says. */
export function endReason(server: Omit<HostRun, 'projectRoot'>): string | null {
  return server.end?.reason ?? server.reason;
}

export type StatusRun = Omit<HostRun, 'projectRoot'> & {readonly projectRoot?: string};

export function liveRuns(listing: Extract<HostListing, {kind: 'listed'}>): readonly StatusRun[] {
  return [...listing.live, ...(listing.unknownProject ?? []).filter(run => run.alive)];
}

export function endedRuns(listing: Extract<HostListing, {kind: 'listed'}>): readonly StatusRun[] {
  return [...listing.ended, ...(listing.unknownProject ?? []).filter(run => run.alive === false)];
}
