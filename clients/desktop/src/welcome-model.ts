/**
 * The welcome view's pure decisions shared by its stores (`welcome/`) and the main process: which
 * hosts the host list shows for a query, where the keyboard selection moves, what the checkout and
 * project fields suggest, and which title-strip controls a window state allows.
 */
import {validAlias} from './host-settings.js';
import type {InstanceRecord} from './instances.js';
import type {RecentRun} from './recent.js';
import {stopKey} from './stop-run.js';
import type {ChromeState} from './welcome-protocol.js';

/** One row of the host list: an alias from `~/.ssh/config`, or the destination the user typed. */
export interface HostChoice {
  readonly alias: string;
  readonly typed: boolean;
}

function usable(alias: string): boolean {
  try {
    validAlias(alias);
    return true;
  } catch {
    return false;
  }
}

/**
 * The hosts matching `query` (case-insensitive substring of the alias), in config order, then the
 * query itself as a typed destination (`user@host` or an alias ssh resolves) when it is usable
 * and not already listed.
 */
export function filterHosts(aliases: readonly string[], query: string): HostChoice[] {
  const needle = query.trim().toLowerCase();
  const matches = aliases
    .filter(alias => alias.toLowerCase().includes(needle))
    .map(alias => ({alias, typed: false}));
  const typed = query.trim();
  if (typed !== '' && usable(typed) && !aliases.includes(typed)) {
    matches.push({alias: typed, typed: true});
  }
  return matches;
}

/** The selection after moving `delta` rows from `index` in a list of `length`, wrapping around. */
export function moveSelection(index: number, delta: number, length: number): number {
  if (length <= 0) return -1;
  const start = index < 0 ? (delta > 0 ? -1 : 0) : index;
  return (((start + delta) % length) + length) % length;
}

/** The checkout to pre-fill for a host: the first `vibesys_root` its live servers report. */
export function suggestCheckout(records: readonly InstanceRecord[]): string | null {
  for (const record of records) {
    if (record.kind === 'compatible' && record.instance.vibesysRoot !== null) {
      return record.instance.vibesysRoot;
    }
  }
  return null;
}

/**
 * The project directories to offer on host `host`: those of its live servers, then those of its
 * recent runs (most recent first), each once.
 */
export function suggestProjects(
  records: readonly InstanceRecord[],
  recent: readonly RecentRun[],
  host: string,
): string[] {
  const projects: string[] = [];
  const add = (project: string): void => {
    if (project !== '' && !projects.includes(project)) projects.push(project);
  };
  for (const record of records) {
    if (record.kind === 'compatible') add(record.instance.projectRoot);
  }
  for (const run of recent) if (run.host === host) add(run.project);
  return projects;
}

/** Which title-strip controls show for the window's state, and the stop line beside them. */
export interface StripControls {
  readonly stop: boolean;
  readonly force: boolean;
  readonly resume: boolean;
  /** Retry the connection: only while it is stuck on something a retry can fix. */
  readonly retry: boolean;
  /** "Back to run": the welcome view fills the window while a run is attached. */
  readonly back: boolean;
  readonly stopText: string;
  readonly stopError: boolean;
}

const NO_STRIP: StripControls = {
  stop: false,
  force: false,
  resume: false,
  retry: false,
  back: false,
  stopText: '',
  stopError: false,
};

export function stripControls(state: ChromeState): StripControls {
  const attached = state.attached;
  if (attached === null) return NO_STRIP;
  const stop =
    attached.instanceId === null
      ? undefined
      : state.stops[stopKey(attached.host, attached.instanceId)];
  const ended = attached.link === 'ended' || stop?.phase === 'ended';
  return {
    stop: attached.instanceId !== null && !ended && stop?.phase !== 'stopping',
    force: !ended && (stop?.canForce ?? false),
    resume: ended,
    retry: attached.link === 'stuck' && !ended,
    back: state.mode === 'welcome',
    stopText: ended ? '' : (stop?.text ?? ''),
    stopError: !ended && stop?.phase === 'error',
  };
}
