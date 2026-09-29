/**
 * Projects and runs for the sidebar. `HomeApi` mirrors the home server's `GET /api/projects` and
 * `GET /api/projects/{id}/runs` (sub-project 2); until it exists the fixture answers, and the open
 * run always appears because the page's own session supplies it.
 */
import type {CoreRunStatus} from '@vibesys/core-state';

/** Plan 2's `GatewayState` for `GET /api/projects/{id}/runs`, verbatim. */
type GatewayState =
  | 'live'
  | 'starting'
  | 'ended_serving'
  | 'failed'
  | 'stale'
  | 'external'
  | 'reopened'
  | 'none';
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
