/**
 * The All runs screen's data: every run of every project this app knows on each connected machine.
 * A machine lists its runs project by project (`runs.list`), so the projects asked are those the
 * app has met on it: its live servers, its ended ones the registry still keeps, and the runs the
 * user opened. Each project's listing carries a token, so a late reply is dropped; the rows are
 * drawn by `allRunsGroups` (`all-runs-rows.ts`), from these listings and the sidebar's digests.
 */
import type {HostKey, HostListing, ListedRun} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';

/** What is known of one project's runs on one machine. */
export type ProjectRunsSlot =
  | {readonly kind: 'loading'; readonly token: number}
  | {readonly kind: 'listed'; readonly runs: readonly ListedRun[]}
  /** The machine did not list the project's runs (unreachable, or the project is gone). */
  | {readonly kind: 'failed'};

export interface AllRunsState {
  /** Each project's listing, by `projectKey`. */
  readonly projects: Readonly<Record<string, ProjectRunsSlot>>;
  /** The last token a listing used, so each is new. */
  readonly lastToken: number;
}

export const ALL_RUNS_EMPTY: AllRunsState = {projects: {}, lastToken: 0};

/** What the All runs screen asks of the main process: one project's runs on one machine. */
export interface ListProjectRuns {
  readonly kind: 'list-project-runs';
  readonly token: number;
  readonly host: HostKey;
  readonly project: string;
}

/** The key of a project's slot on a machine. */
function projectKey(host: HostKey, project: string): string {
  return JSON.stringify([host, project]);
}

/** The projects the app has met on `host`, each once: live, then ended, then opened by the user. */
function knownProjects(
  listing: HostListing | undefined,
  recent: readonly RecentRun[],
  host: HostKey,
): string[] {
  const projects = new Set<string>();
  if (listing?.kind === 'listed') {
    for (const run of [...listing.live, ...listing.ended]) projects.add(run.projectRoot);
  }
  for (const run of recent) if (run.host === host) projects.add(run.project);
  projects.delete('');
  return [...projects];
}

/**
 * The listings the screen still needs: one per known project of each connected machine that has no
 * slot yet. Returns the slots to add and the requests that fill them.
 */
export function askProjectRuns(
  all: AllRunsState,
  listings: readonly HostListing[],
  recent: readonly RecentRun[],
): {readonly all: AllRunsState; readonly requests: readonly ListProjectRuns[]} {
  const projects: Record<string, ProjectRunsSlot> = {...all.projects};
  const requests: ListProjectRuns[] = [];
  let token = all.lastToken;
  for (const listing of listings) {
    if (listing.kind !== 'listed') continue;
    for (const project of knownProjects(listing, recent, listing.host)) {
      const key = projectKey(listing.host, project);
      if (projects[key] !== undefined) continue;
      token += 1;
      projects[key] = {kind: 'loading', token};
      requests.push({kind: 'list-project-runs', token, host: listing.host, project});
    }
  }
  if (requests.length === 0) return {all, requests};
  return {all: {projects, lastToken: token}, requests};
}

/** What the machine said of `project`'s runs asked under `token`; null when it could not say. */
export function projectRunsListed(
  all: AllRunsState,
  answer: {
    readonly token: number;
    readonly host: HostKey;
    readonly project: string;
    readonly runs: readonly ListedRun[] | null;
  },
): AllRunsState {
  const key = projectKey(answer.host, answer.project);
  const slot = all.projects[key];
  if (slot?.kind !== 'loading' || slot.token !== answer.token) return all;
  const next: ProjectRunsSlot =
    answer.runs === null ? {kind: 'failed'} : {kind: 'listed', runs: answer.runs};
  return {...all, projects: {...all.projects, [key]: next}};
}

/** A run of a project on a machine, named without its listing. */
export interface ProjectRunRef {
  readonly host: HostKey;
  readonly project: string;
  readonly run: ListedRun;
}

/** Every run the listings hold, with where it is. */
export function listedRunRefs(all: AllRunsState): ProjectRunRef[] {
  return Object.entries(all.projects).flatMap(([key, slot]) => {
    if (slot.kind !== 'listed') return [];
    const [host, project] = JSON.parse(key) as [HostKey, string];
    return slot.runs.map(run => ({host, project, run}));
  });
}

/** The slots of the projects listed, or being listed, on `host`. */
export function hostSlots(all: AllRunsState, host: HostKey): ProjectRunsSlot[] {
  return Object.entries(all.projects)
    .filter(([key]) => (JSON.parse(key) as [HostKey, string])[0] === host)
    .map(([, slot]) => slot);
}
