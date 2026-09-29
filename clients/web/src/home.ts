/**
 * Projects and runs for the sidebar. `HomeApi` is the home server's `GET /api/projects` and
 * `GET /api/projects/{id}/runs`; `fixtureHomeApi` answers on pages without a home server (replay,
 * a gateway's own page), where the open run is listed from the page's own session.
 */
import type {CoreRunStatus} from '@vibesys/core-state';
import type {GatewayState, HomeClient, RunRow} from './home-api.js';
import {homeHref, runHref} from './route.js';

type RunOutcome = 'running' | 'paused' | 'completed' | 'failed' | 'stopped' | null;

export interface HomeProject {
  id: string;
  name: string;
  path: string;
}

export interface HomeRun {
  id: string;
  projectId: string;
  title: string;
  gateway: GatewayState;
  outcome: RunOutcome;
  updatedAt: string;
  /** Capability URL of a reachable gateway; null when the run cannot be opened from here. */
  url: string | null;
}

export interface HomeApi {
  projects(): Promise<HomeProject[]>;
  runs(projectId: string): Promise<HomeRun[]>;
}

export function fixtureHomeApi(projects: HomeProject[] = [], runs: HomeRun[] = []): HomeApi {
  return {
    projects: async () => projects,
    runs: async projectId => runs.filter(run => run.projectId === projectId),
  };
}

export interface OpenRun {
  run: HomeRun;
  projectName: string;
}

const OUTCOMES: Partial<Record<CoreRunStatus, RunOutcome>> = {
  starting: 'running',
  running: 'running',
  pausing: 'running',
  stopping: 'running',
  paused: 'paused',
  completed: 'completed',
  failed: 'failed',
  interrupted: 'failed',
  stopped: 'stopped',
};

/** The page's own run as a sidebar row. */
export function openRun(input: {
  runId: string | null;
  title: string;
  project: string | null;
  status: CoreRunStatus;
  updatedAt: string | null;
}): OpenRun | null {
  if (input.runId === null) return null;
  const outcome = OUTCOMES[input.status] ?? null;
  const ended = outcome === 'completed' || outcome === 'failed' || outcome === 'stopped';
  const projectName = input.project ?? 'This run';
  return {
    projectName,
    run: {
      id: input.runId,
      projectId: projectName,
      title: input.title,
      gateway: ended ? 'ended_serving' : 'live',
      outcome,
      updatedAt: input.updatedAt ?? '',
      url: null,
    },
  };
}

export interface ProjectSection {
  id: string;
  name: string;
  runs: HomeRun[];
}

/** Projects as the sidebar lists them; the open run replaces its own row, or leads its project. */
export function sidebarSections(
  projects: readonly HomeProject[],
  runs: readonly HomeRun[],
  open: OpenRun | null,
): ProjectSection[] {
  const sections = projects.map(project => ({
    id: project.id,
    name: project.name,
    runs: runs
      .filter(run => run.projectId === project.id)
      .map(run => (open !== null && run.id === open.run.id ? open.run : run)),
  }));
  if (open === null || sections.some(section => section.runs.includes(open.run))) return sections;
  const home = sections.find(section => section.name === open.projectName);
  if (home !== undefined) {
    home.runs.unshift(open.run);
    return sections;
  }
  return [{id: open.run.projectId, name: open.projectName, runs: [open.run]}, ...sections];
}

const UNITS: ReadonlyArray<readonly [number, string]> = [
  [604_800, 'w'],
  [86_400, 'd'],
  [3_600, 'h'],
  [60, 'm'],
];

export function relativeTime(iso: string, now: Date): string {
  const seconds = (now.getTime() - Date.parse(iso)) / 1000;
  if (!Number.isFinite(seconds)) return 'now';
  for (const [size, unit] of UNITS) {
    if (seconds >= size) return `${Math.floor(seconds / size)}${unit}`;
  }
  return 'now';
}

export interface Listing {
  projects: HomeProject[];
  runs: HomeRun[];
}

export const EMPTY_LISTING: Listing = {projects: [], runs: []};

/** Every project, and the runs of each project that answered: one failing project drops only its runs. */
export async function loadListing(home: HomeApi): Promise<Listing> {
  const projects = await home.projects().catch((): HomeProject[] => []);
  const settled = await Promise.allSettled(projects.map(project => home.runs(project.id)));
  return {
    projects,
    runs: settled.flatMap(result => (result.status === 'fulfilled' ? result.value : [])),
  };
}

const LOOP_TITLES: Readonly<Record<string, string>> = {
  agent: 'Agent run',
  'profile-guided': 'Profile-guided run',
  dynamic: 'Dynamic run',
  plain: 'Plain run',
  evolve: 'Evolve run',
};

/** The objective's first line, as the run page titles itself; else the task; else the loop. */
function rowTitle(row: RunRow): string {
  const first = row.objective?.split('\n', 1)[0]?.trim() ?? '';
  if (first !== '') return first;
  if (row.task !== null) return row.task;
  return (row.loop === null ? undefined : LOOP_TITLES[row.loop]) ?? 'Run';
}

const ROW_OUTCOMES: Record<RunRow['status'], RunOutcome> = {
  active: 'running',
  completed: 'completed',
  failed: 'failed',
  unknown: null,
};

/**
 * Where a row leads: a gateway this origin may use opens the run page; an external gateway only
 * accepts its own page; a finished run without one reopens; a launch in flight or a failed launch
 * has nothing to open yet.
 */
function rowUrl(row: RunRow, projectId: string, token: string): string | null {
  if (row.gateway.state === 'external') return row.gateway.url;
  const serving = [row.gateway, row.reopen].find(
    gateway =>
      gateway !== null &&
      gateway.websocket_url !== null &&
      !gateway.origin_mismatch &&
      gateway.state !== 'failed' &&
      gateway.state !== 'stale',
  );
  if (serving?.websocket_url != null) return runHref(token, projectId, serving.websocket_url);
  if (row.status === 'active' || row.gateway.state === 'failed') return null;
  return homeHref(token, {kind: 'open', projectId, runId: row.run_id});
}

export function homeRun(row: RunRow, projectId: string, token: string): HomeRun {
  return {
    id: row.run_id,
    projectId,
    title: rowTitle(row),
    gateway: row.gateway.state,
    outcome: ROW_OUTCOMES[row.status],
    updatedAt: row.created_at ?? '',
    url: rowUrl(row, projectId, token),
  };
}

export function httpHomeApi(client: HomeClient, token: string): HomeApi {
  return {
    projects: async () =>
      (await client.projects()).projects.map(project => ({
        id: project.id,
        name: project.name,
        path: project.root,
      })),
    runs: async projectId =>
      (await client.runs(projectId)).runs.map(row => homeRun(row, projectId, token)),
  };
}
