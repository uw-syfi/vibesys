/**
 * Where the person is in the whole app: a destination of the rail (the runs, the cost, the machines, or the settings) and,
 * for each, where they were last. Leaving a destination keeps its place, so Settings and back
 * returns to the run that was open. A plain reducer, so every move is a value and a test can walk
 * any sequence of them.
 */
import {
  type FleetFilter,
  type FleetState,
  fleetKey,
  NO_FLEET_FILTER,
} from '../orchestrator/fleet.js';
import {initialRoute, type Route, type RouteAction, routeReducer} from '../route.js';

/** The rail's destinations. */
export type Destination = 'runs' | 'cost' | 'machines' | 'settings';

/** What the run list shows and where it was left: kept while a run, or another destination, shows. */
export interface FleetPlace extends FleetFilter {
  /** The `fleetKey` of the run last opened from the list, so going back finds the row again. */
  readonly selected: string | null;
}

export interface Navigation {
  readonly destination: Destination;
  /** Where the runs destination is: its list, or one run. Kept while another destination shows. */
  readonly runs: Route;
  /** The run list's filters and selected row. Kept while a run or another destination shows. */
  readonly fleet: FleetPlace;
}

export type NavigationAction =
  | RouteAction
  /** The rail's Runs: the runs destination, at its list when it was already showing. */
  | {readonly type: 'open-runs'}
  /** The list's state filter: all runs, the live ones or the ones that are over. */
  | {readonly type: 'filter-runs-by-state'; readonly state: FleetState}
  /** The list's host filter: one host, or null for every host. */
  | {readonly type: 'filter-runs-by-host'; readonly host: string | null}
  /** The rail's Cost: the cost of every run together. */
  | {readonly type: 'open-cost'}
  /** The rail's Machines: every machine the app is connected to. */
  | {readonly type: 'open-machines'}
  | {readonly type: 'open-settings'}
  /** Escape or Alt+Left: one step back, never out of the app. */
  | {readonly type: 'back'};

export type NavigationKind = NavigationAction['type'];

/** Every kind of `NavigationAction`; the type makes a kind added to the union a compile error here. */
const KINDS = {
  'open-run': true,
  'close-run': true,
  show: true,
  'show-ideas': true,
  'pick-agent': true,
  'close-agent': true,
  'open-runs': true,
  'filter-runs-by-state': true,
  'filter-runs-by-host': true,
  'open-cost': true,
  'open-machines': true,
  'open-settings': true,
  back: true,
} as const satisfies Record<NavigationKind, true>;

/** Each kind of `NavigationAction`, so a test can hold every one to a control that sends it. */
export const NAVIGATION_KINDS = Object.keys(KINDS) as readonly NavigationKind[];

export const initialNavigation: Navigation = {
  destination: 'runs',
  runs: initialRoute,
  fleet: {...NO_FLEET_FILTER, selected: null},
};

/**
 * One step back inside the runs: the agent first, then a screen of the run's own back to its report,
 * then the run itself (the report is where a run opens, so it is the last step before the list).
 */
function stepBack(route: Route): Route {
  if (route.screen === 'runs') return route;
  if (route.agentId !== null) return routeReducer(route, {type: 'close-agent'});
  if (route.view !== 'report') return routeReducer(route, {type: 'show', view: 'report'});
  return routeReducer(route, {type: 'close-run'});
}

/** Whether the run list is what the person is looking at: the only place its filters are. */
const onList = (navigation: Navigation): boolean =>
  navigation.destination === 'runs' && navigation.runs.screen === 'runs';

export function navigationReducer(navigation: Navigation, action: NavigationAction): Navigation {
  switch (action.type) {
    case 'open-run':
      // The row opened is the one to find again on the way back.
      return navigation.destination === 'runs'
        ? {
            ...navigation,
            runs: routeReducer(navigation.runs, action),
            fleet: {...navigation.fleet, selected: fleetKey(action.host, action.runId)},
          }
        : navigation;
    case 'close-run':
    case 'show':
    case 'show-ideas':
    case 'pick-agent':
    case 'close-agent':
      // These come from the runs screens, so they mean nothing while another destination shows.
      return navigation.destination === 'runs'
        ? {...navigation, runs: routeReducer(navigation.runs, action)}
        : navigation;
    case 'open-runs':
      return {
        ...navigation,
        destination: 'runs',
        runs: navigation.destination === 'runs' ? initialRoute : navigation.runs,
      };
    case 'filter-runs-by-state':
      // The list's own controls: they mean nothing while a run or another destination shows.
      return onList(navigation)
        ? {...navigation, fleet: {...navigation.fleet, state: action.state}}
        : navigation;
    case 'filter-runs-by-host':
      return onList(navigation)
        ? {...navigation, fleet: {...navigation.fleet, host: action.host}}
        : navigation;
    case 'open-cost':
      return {...navigation, destination: 'cost'};
    case 'open-machines':
      return {...navigation, destination: 'machines'};
    case 'open-settings':
      return {...navigation, destination: 'settings'};
    case 'back':
      return navigation.destination === 'runs'
        ? {...navigation, runs: stepBack(navigation.runs)}
        : {...navigation, destination: 'runs'};
  }
}
