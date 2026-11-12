/**
 * Where the person is in the whole app: a destination of the rail (the runs, the cost, the machines, or the settings) and,
 * for each, where they were last. Leaving a destination keeps its place, so Settings and back
 * returns to the run that was open. A plain reducer, so every move is a value and a test can walk
 * any sequence of them.
 */
import {
  type FleetColumn,
  type FleetFilter,
  type FleetSort,
  type FleetState,
  fleetKey,
  NO_FLEET_FILTER,
  pressedSort,
} from '../orchestrator/fleet.js';
import {initialRoute, type Route, type RouteAction, routeReducer} from '../route.js';

/** The rail's destinations. */
export type Destination = 'runs' | 'cost' | 'machines' | 'settings';

/** The Machines destination's own place; Add remembers where its breadcrumb returns. */
export type MachinesPlace =
  | {readonly screen: 'list'}
  | {readonly screen: 'add'; readonly origin: 'machines' | 'new-run'};

/** What the run list shows and where it was left: kept while a run, or another destination, shows. */
export interface FleetPlace extends FleetFilter {
  /** The `fleetKey` of the run last opened from the list, so going back finds the row again. */
  readonly selected: string | null;
  /** How the list is sorted, or null for its own order (newest first). */
  readonly sort: FleetSort | null;
  /** The widths, in px, a person gave columns by dragging their edges, by column key. */
  readonly widths: Readonly<Record<string, number>>;
  /** Explicit operator receipts by fleet key; absence has never been opened in this app session. */
  readonly receipts: Readonly<Record<string, RunReceipt>>;
}

export type RunReceipt =
  | {readonly kind: 'read'; readonly activityKey: string}
  | {readonly kind: 'unread'};

/** A row needs the operator when it is new, explicitly unread, or has changed since it was read. */
export const runIsUnread = (receipt: RunReceipt | undefined, activityKey: string): boolean =>
  receipt?.kind !== 'read' || receipt.activityKey !== activityKey;

export interface Navigation {
  readonly destination: Destination;
  /** Where the runs destination is: its list, or one run. Kept while another destination shows. */
  readonly runs: Route;
  /** The run list's filters and selected row. Kept while a run or another destination shows. */
  readonly fleet: FleetPlace;
  readonly machines: MachinesPlace;
}

export type NavigationAction =
  | RouteAction
  /** The rail's Runs: the runs destination, at its list when it was already showing. */
  | {readonly type: 'open-runs'}
  /** The list's state filter: all runs, the live ones or the ones that are over. */
  | {readonly type: 'filter-runs-by-state'; readonly state: FleetState}
  /** The list's host filter: one host, or null for every host. */
  | {readonly type: 'filter-runs-by-host'; readonly host: string | null}
  /** A press on a column's header: sorts by it, then the other way, then not at all. */
  | {readonly type: 'sort-runs'; readonly column: FleetColumn}
  /** A column's edge dragged: its width in px, or null to put it back to its own. */
  | {readonly type: 'resize-runs-column'; readonly column: string; readonly width: number | null}
  /** The list's operator query, kept with the other list filters. */
  | {readonly type: 'filter-runs-by-query'; readonly query: string}
  /** Record one exact structured revision as read, without opening it. */
  | {
      readonly type: 'mark-run-read';
      readonly host: string;
      readonly runId: string;
      readonly activityKey: string;
    }
  /** Keep a run visually prominent until it is opened or explicitly marked read. */
  | {readonly type: 'mark-run-unread'; readonly host: string; readonly runId: string}
  /** The rail's Cost: the cost of every run together. */
  | {readonly type: 'open-cost'}
  /** The rail's Machines: every machine the app is connected to. */
  | {readonly type: 'open-machines'}
  /** A dedicated Add machine page, entered from Machines or from the New run flow. */
  | {readonly type: 'open-add-machine'; readonly origin: 'machines' | 'new-run'}
  /** Leave Add machine for its recorded origin. */
  | {readonly type: 'close-add-machine'}
  | {readonly type: 'open-settings'}
  /** Escape or Alt+Left: one step back, never out of the app. */
  | {readonly type: 'back'};

export type NavigationKind = NavigationAction['type'];

/** Every kind of `NavigationAction`; the type makes a kind added to the union a compile error here. */
const KINDS = {
  'open-new-run': true,
  'open-run': true,
  'close-run': true,
  show: true,
  'show-ideas': true,
  'pick-agent': true,
  'close-agent': true,
  'open-runs': true,
  'filter-runs-by-state': true,
  'filter-runs-by-host': true,
  'sort-runs': true,
  'resize-runs-column': true,
  'filter-runs-by-query': true,
  'mark-run-read': true,
  'mark-run-unread': true,
  'open-cost': true,
  'open-machines': true,
  'open-add-machine': true,
  'close-add-machine': true,
  'open-settings': true,
  back: true,
} as const satisfies Record<NavigationKind, true>;

/** Each kind of `NavigationAction`, so a test can hold every one to a control that sends it. */
export const NAVIGATION_KINDS = Object.keys(KINDS) as readonly NavigationKind[];

export const initialNavigation: Navigation = {
  destination: 'runs',
  runs: initialRoute,
  fleet: {...NO_FLEET_FILTER, selected: null, sort: null, widths: {}, receipts: {}},
  machines: {screen: 'list'},
};

/**
 * One step back inside the runs: the agent first, then a screen of the run's own back to its report,
 * then the run itself (the report is where a run opens, so it is the last step before the list).
 */
function stepBack(route: Route): Route {
  if (route.screen === 'runs') return route;
  if (route.screen === 'new-run') return routeReducer(route, {type: 'close-run'});
  if (route.agentId !== null) return routeReducer(route, {type: 'close-agent'});
  if (route.view !== 'report') return routeReducer(route, {type: 'show', view: 'report'});
  return routeReducer(route, {type: 'close-run'});
}

/** Whether the run list is what the person is looking at: the only place its filters are. */
const onList = (navigation: Navigation): boolean =>
  navigation.destination === 'runs' && navigation.runs.screen === 'runs';

/** The list's own arrangement after a press on a header or a drag of a column's edge. */
function arrange(
  fleet: FleetPlace,
  action: Extract<NavigationAction, {type: 'sort-runs' | 'resize-runs-column'}>,
): FleetPlace {
  if (action.type === 'sort-runs') return {...fleet, sort: pressedSort(fleet.sort, action.column)};
  const {[action.column]: _, ...rest} = fleet.widths;
  const given = action.width;
  // A width that is no width (not a positive number) puts the column back like null does.
  return given === null || !Number.isFinite(given) || given <= 0
    ? {...fleet, widths: rest}
    : {...fleet, widths: {...rest, [action.column]: Math.round(given)}};
}

function routeNavigationReducer(navigation: Navigation, action: RouteAction): Navigation {
  switch (action.type) {
    case 'open-run':
      // A run opens from the list and from the Cost page's rows: the runs destination shows it. The
      // row opened is the one to find again on the way back.
      return {
        ...navigation,
        destination: 'runs',
        runs: routeReducer(navigation.runs, action),
        machines: {screen: 'list'},
        fleet: {
          ...navigation.fleet,
          selected: fleetKey(action.host, action.runId),
          receipts:
            action.activityKey === undefined
              ? navigation.fleet.receipts
              : {
                  ...navigation.fleet.receipts,
                  [fleetKey(action.host, action.runId)]: {
                    kind: 'read',
                    activityKey: action.activityKey,
                  },
                },
        },
      };
    case 'open-new-run':
      // The list's New run: the runs destination shows the page that starts one.
      return {
        ...navigation,
        destination: 'runs',
        runs: routeReducer(navigation.runs, action),
        machines: {screen: 'list'},
      };
    case 'close-run':
    case 'show':
    case 'show-ideas':
    case 'pick-agent':
    case 'close-agent':
      // These come from the runs screens, so they mean nothing while another destination shows.
      return navigation.destination === 'runs'
        ? {...navigation, runs: routeReducer(navigation.runs, action)}
        : navigation;
  }
}

type FleetNavigationAction = Extract<
  NavigationAction,
  {
    readonly type:
      | 'filter-runs-by-state'
      | 'filter-runs-by-host'
      | 'filter-runs-by-query'
      | 'sort-runs'
      | 'resize-runs-column'
      | 'mark-run-read'
      | 'mark-run-unread';
  }
>;

function fleetNavigationReducer(navigation: Navigation, action: FleetNavigationAction): Navigation {
  if (!onList(navigation)) return navigation;
  switch (action.type) {
    case 'filter-runs-by-state':
      return {...navigation, fleet: {...navigation.fleet, state: action.state}};
    case 'filter-runs-by-host':
      return {...navigation, fleet: {...navigation.fleet, host: action.host}};
    case 'filter-runs-by-query':
      return {...navigation, fleet: {...navigation.fleet, query: action.query}};
    case 'sort-runs':
    case 'resize-runs-column':
      return {...navigation, fleet: arrange(navigation.fleet, action)};
    case 'mark-run-read':
      return {
        ...navigation,
        fleet: {
          ...navigation.fleet,
          receipts: {
            ...navigation.fleet.receipts,
            [fleetKey(action.host, action.runId)]: {
              kind: 'read',
              activityKey: action.activityKey,
            },
          },
        },
      };
    case 'mark-run-unread':
      return {
        ...navigation,
        fleet: {
          ...navigation.fleet,
          receipts: {
            ...navigation.fleet.receipts,
            [fleetKey(action.host, action.runId)]: {kind: 'unread'},
          },
        },
      };
  }
}

function closeAddMachine(navigation: Navigation): Navigation {
  if (navigation.machines.screen !== 'add') return navigation;
  return navigation.machines.origin === 'new-run'
    ? {...navigation, destination: 'runs', machines: {screen: 'list'}}
    : {...navigation, destination: 'machines', machines: {screen: 'list'}};
}

type DestinationNavigationAction = Exclude<NavigationAction, RouteAction | FleetNavigationAction>;

function destinationNavigationReducer(
  navigation: Navigation,
  action: DestinationNavigationAction,
): Navigation {
  switch (action.type) {
    case 'open-runs':
      return {
        ...navigation,
        destination: 'runs',
        runs: navigation.destination === 'runs' ? initialRoute : navigation.runs,
        machines: {screen: 'list'},
      };
    case 'open-cost':
      return {...navigation, destination: 'cost', machines: {screen: 'list'}};
    case 'open-machines':
      return {...navigation, destination: 'machines', machines: {screen: 'list'}};
    case 'open-add-machine':
      return {
        ...navigation,
        destination: action.origin === 'new-run' ? 'runs' : 'machines',
        ...(action.origin === 'new-run' ? {runs: {screen: 'new-run'} as const} : {}),
        machines: {screen: 'add', origin: action.origin},
      };
    case 'close-add-machine':
      return closeAddMachine(navigation);
    case 'open-settings':
      return {...navigation, destination: 'settings', machines: {screen: 'list'}};
    case 'back':
      if (navigation.machines.screen === 'add') return closeAddMachine(navigation);
      if (navigation.destination === 'runs')
        return {...navigation, runs: stepBack(navigation.runs)};
      return {...navigation, destination: 'runs'};
  }
}

export function navigationReducer(navigation: Navigation, action: NavigationAction): Navigation {
  switch (action.type) {
    case 'open-run':
    case 'open-new-run':
    case 'close-run':
    case 'show':
    case 'show-ideas':
    case 'pick-agent':
    case 'close-agent':
      return routeNavigationReducer(navigation, action);
    case 'filter-runs-by-state':
    case 'filter-runs-by-host':
    case 'filter-runs-by-query':
    case 'sort-runs':
    case 'resize-runs-column':
    case 'mark-run-read':
    case 'mark-run-unread':
      return fleetNavigationReducer(navigation, action);
    case 'open-runs':
    case 'open-cost':
    case 'open-machines':
    case 'open-add-machine':
    case 'close-add-machine':
    case 'open-settings':
    case 'back':
      return destinationNavigationReducer(navigation, action);
  }
}
