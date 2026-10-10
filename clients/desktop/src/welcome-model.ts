/**
 * The welcome view's decisions, as pure functions: which hosts the host list shows for a query,
 * where the keyboard selection moves, and what the checkout and project fields suggest. The page
 * and the main process only render and fetch; every choice is made here and tested here.
 */
import {validAlias} from './host-settings.js';
import type {InstanceRecord} from './instances.js';
import type {RecentRun} from './recent.js';

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
