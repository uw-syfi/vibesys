/**
 * What the welcome view's fields suggest, from a host's live records and the recent runs: the
 * checkout to pre-fill and the project directories to offer.
 */
import type {InstanceRecord} from './instances.js';
import type {RecentRun} from './recent.js';

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
