/**
 * What a registry record says about its run, in the sidebar's words: a live run is running or paused
 * (`paused` names who paused it); an ended one finished, failed or stopped, from its end record's
 * outcome, or from its last phase on a tombstone that records no end.
 */
import type {HostRun} from '@vibesys/hosts';

/** What a sidebar row says about its run; never shown by color alone. */
export type RunRowStatus = 'live' | 'paused' | 'finished' | 'failed' | 'stopped' | 'unknown';

type Outcome = NonNullable<HostRun['end']>['outcome'];

const OUTCOMES: Readonly<Record<Outcome, RunRowStatus>> = {
  adopted: 'finished',
  'kept-input': 'finished',
  'no-winner': 'finished',
  deadline: 'finished',
  'finish-refused': 'finished',
  'adoption-failed': 'failed',
  failed: 'failed',
  stopped: 'stopped',
  force_stopped: 'stopped',
};

/** The status of a live server's run. */
export function liveStatus(server: HostRun): RunRowStatus {
  return server.paused === null ? 'live' : 'paused';
}

/** The status of an ended server's run, or `unknown` when there is no record of it. */
export function endedStatus(server: HostRun | undefined): RunRowStatus {
  if (server === undefined) return 'unknown';
  if (server.end !== null) return OUTCOMES[server.end.outcome];
  switch (server.phase) {
    case 'completed':
      return 'finished';
    case 'failed':
      return 'failed';
    case 'stopped':
      return 'stopped';
    default:
      return 'unknown';
  }
}

const WORDS: Readonly<Record<RunRowStatus, string>> = {
  live: 'running',
  paused: 'paused',
  finished: 'finished',
  failed: 'failed',
  stopped: 'stopped',
  unknown: 'status unknown',
};

/** The word for a row's status. */
export function statusWord(status: RunRowStatus): string {
  return WORDS[status];
}

/** A live run's detail: its phase in words, or who paused it. */
export function liveDetail(server: HostRun): string {
  if (server.paused === 'quota') return 'paused: usage limit';
  if (server.paused === 'user') return 'paused by you';
  return server.status;
}

/** Why a run ended, in one line, when its record says. */
export function endReason(server: HostRun): string | null {
  return server.end?.reason ?? server.reason;
}
