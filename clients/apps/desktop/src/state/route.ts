/**
 * Where the user is: the run list, the page that starts a run, or one page of one run: its report, its agents, one agent's
 * transcript (the agents view with an agent open), its cost or its catch-up. A plain reducer, so
 * every move is a value and a test can walk any sequence of them.
 */
import type {IdeaWindow} from './orchestrator/report/ideas.js';

export type RunView = 'report' | 'agents' | 'cost' | 'catch-up';

export type Route =
  | {readonly screen: 'runs'}
  /** The page that starts a run: its own steps are the page's (`state/new-run`), not the route's. */
  | {readonly screen: 'new-run'}
  | {
      readonly screen: 'run';
      readonly host: string;
      readonly runId: string;
      readonly view: RunView;
      /** The agent whose transcript is the page, under the agents view; null on every other page. */
      readonly agentId: string | null;
      /** The span of a check-in the report's ideas are narrowed to (a display filter); null for all. */
      readonly ideas: IdeaWindow | null;
    };

export type RouteAction =
  | {readonly type: 'open-new-run'}
  | {
      readonly type: 'open-run';
      readonly host: string;
      readonly runId: string;
      /** The Runs card revision being opened; navigation records it as read when present. */
      readonly activityKey?: string;
    }
  | {readonly type: 'close-run'}
  | {readonly type: 'show'; readonly view: RunView}
  | {readonly type: 'show-ideas'; readonly window: IdeaWindow}
  | {readonly type: 'pick-agent'; readonly agentId: string}
  | {readonly type: 'close-agent'};

export const initialRoute: Route = {screen: 'runs'};

export function routeReducer(route: Route, action: RouteAction): Route {
  switch (action.type) {
    case 'open-new-run':
      return {screen: 'new-run'};
    case 'open-run':
      return {
        screen: 'run',
        host: action.host,
        runId: action.runId,
        view: 'report',
        agentId: null,
        ideas: null,
      };
    case 'close-run':
      return initialRoute;
    case 'show':
      // A page of the run replaces the one before it, an agent's transcript too.
      return route.screen === 'run'
        ? {...route, view: action.view, agentId: null, ideas: null}
        : route;
    case 'show-ideas':
      // The report, with its ideas narrowed to what a check-in covers.
      return route.screen === 'run'
        ? {...route, view: 'report', agentId: null, ideas: action.window}
        : route;
    case 'pick-agent':
      // An agent is under the agents view: closing it lands there.
      return route.screen === 'run'
        ? {...route, view: 'agents', agentId: action.agentId, ideas: null}
        : route;
    case 'close-agent':
      return route.screen === 'run' ? {...route, agentId: null} : route;
  }
}

/** The run whose cost stream a screen needs open. */
export interface CostTarget {
  readonly host: string;
  readonly runId: string;
}

/**
 * Which run's cost stream to keep open: the run whose report or Cost view is showing (the report
 * carries the run's spend in brief), and none while any other view, the run list or an agent opened
 * over the views is. The stream costs the host work and the view is only read where it is shown, so
 * nothing else follows it.
 */
export function costTarget(route: Route): CostTarget | null {
  return route.screen === 'run' &&
    (route.view === 'cost' || route.view === 'report') &&
    route.agentId === null
    ? {host: route.host, runId: route.runId}
    : null;
}
