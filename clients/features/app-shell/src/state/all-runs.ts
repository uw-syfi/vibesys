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
export interface ProjectRunRefusal {
  readonly kind: 'unreachable';
  readonly detail: string;
}

type ProjectRunsSlot =
  | {readonly kind: 'loading'; readonly token: number; readonly refusal?: ProjectRunRefusal}
  | {readonly kind: 'listed'; readonly runs: readonly ListedRun[]}
  /** The machine did not list the project's runs (unreachable, or the project is gone). */
  | {readonly kind: 'failed'; readonly refusal: ProjectRunRefusal};

export interface AllRunsState {
  /** Each project's listing, by `projectKey`. */
  readonly projects: Readonly<Record<string, ProjectRunsSlot>>;
  /**
   * What each project listed before the screen asked again, by `projectKey`: the runs shown until
   * the new listing answers (or when it cannot), so a refresh never empties the dashboard.
   */
  readonly stale: Readonly<Record<string, readonly ListedRun[]>>;
  /** The last token a listing used, so each is new. */
  readonly lastToken: number;
}

export const ALL_RUNS_EMPTY: AllRunsState = {projects: {}, stale: {}, lastToken: 0};

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
  retryFailed = false,
): {readonly all: AllRunsState; readonly requests: readonly ListProjectRuns[]} {
  const projects: Record<string, ProjectRunsSlot> = {...all.projects};
  const requests: ListProjectRuns[] = [];
  let token = all.lastToken;
  for (const listing of listings) {
    if (listing.kind !== 'listed') continue;
    for (const project of knownProjects(listing, recent, listing.host)) {
      const key = projectKey(listing.host, project);
      const previous = projects[key];
      if (previous !== undefined && !(retryFailed && previous.kind === 'failed')) continue;
      token += 1;
      projects[key] = {
        kind: 'loading',
        token,
        ...(previous?.kind === 'failed' ? {refusal: previous.refusal} : {}),
      };
      requests.push({kind: 'list-project-runs', token, host: listing.host, project});
    }
  }
  if (requests.length === 0) return {all, requests};
  return {all: {...all, projects, lastToken: token}, requests};
}

/** What the machine said of `project`'s runs asked under `token`; null when it could not say. */
export function projectRunsListed(
  all: AllRunsState,
  answer: {
    readonly token: number;
    readonly host: HostKey;
    readonly project: string;
    readonly runs: readonly ListedRun[] | null;
    readonly error?: string;
  },
): AllRunsState {
  const key = projectKey(answer.host, answer.project);
  const slot = all.projects[key];
  if (slot?.kind !== 'loading' || slot.token !== answer.token) return all;
  const next: ProjectRunsSlot =
    answer.runs === null
      ? {
          kind: 'failed',
          refusal: {kind: 'unreachable', detail: answer.error ?? 'The project could not be listed'},
        }
      : {kind: 'listed', runs: answer.runs};
  return {...all, projects: {...all.projects, [key]: next}};
}

/** A run of a project on a machine, named without its listing. */
export interface ProjectRunRef {
  readonly host: HostKey;
  readonly project: string;
  readonly run: ListedRun;
}

/**
 * Asking every project again: what each had listed is kept as the runs to show until its new
 * listing answers, and the next `askProjectRuns` asks for them all.
 */
export function refreshed(all: AllRunsState): AllRunsState {
  const stale: Record<string, readonly ListedRun[]> = {...all.stale};
  for (const [key, slot] of Object.entries(all.projects)) {
    if (slot.kind === 'listed') stale[key] = slot.runs;
  }
  return {...all, projects: {}, stale};
}

/**
 * Every run the listings hold, with where it is: a project not listed again yet keeps its last
 * listing. A run is one entry per machine however many projects name it (the app may remember one
 * run under two spellings of its directory): the first listing to hold it speaks.
 */
function listedRunRefs(all: AllRunsState): ProjectRunRef[] {
  const keys = new Set([...Object.keys(all.projects), ...Object.keys(all.stale)]);
  const seen = new Set<string>();
  return [...keys].flatMap(key => {
    const slot = all.projects[key];
    const runs = slot?.kind === 'listed' ? slot.runs : (all.stale[key] ?? []);
    const [host, project] = JSON.parse(key) as [HostKey, string];
    return runs.flatMap(run => {
      const id = JSON.stringify([host, run.runId]);
      if (seen.has(id)) return [];
      seen.add(id);
      return [{host, project, run}];
    });
  });
}

/**
 * Every run the dashboard can list: those the project listings hold, and those a machine's own
 * registry reports (live and recently ended) that no listing holds yet. A machine's report needs
 * no local record and no second question, so a run it reports is listed and openable at once; a
 * project listing, when it has the run, says more of it (its name, its task, when it began).
 */
export function reportedRunRefs(
  all: AllRunsState,
  listings: readonly HostListing[],
): ProjectRunRef[] {
  const refs = listedRunRefs(all);
  const known = new Set(refs.map(({host, run}) => JSON.stringify([host, run.runId])));
  for (const listing of listings) {
    if (listing.kind !== 'listed') continue;
    for (const server of [...listing.live, ...listing.ended]) {
      const key = JSON.stringify([listing.host, server.runId]);
      if (known.has(key) || server.projectRoot === '') continue;
      known.add(key);
      refs.push({
        host: listing.host,
        project: server.projectRoot,
        run: {runId: server.runId, task: null, createdAt: server.startedAt * 1000},
      });
    }
  }
  return refs;
}

/** Missing project listings remain named while waiting, after refusal, and during retry. */
export function projectRunGaps(all: AllRunsState): readonly {
  readonly host: HostKey;
  readonly project: string;
  readonly refusal: ProjectRunRefusal | null;
}[] {
  return Object.entries(all.projects).flatMap(([key, slot]) => {
    if (slot.kind === 'listed') return [];
    const [host, project] = JSON.parse(key) as [HostKey, string];
    return [{host, project, refusal: slot.refusal ?? null}];
  });
}
