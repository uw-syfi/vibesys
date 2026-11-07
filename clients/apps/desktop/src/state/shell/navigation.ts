/**
 * Where the person is in the whole app: a destination of the rail (the runs, or the settings) and,
 * for each, where they were last. Leaving a destination keeps its place, so Settings and back
 * returns to the run that was open. A plain reducer, so every move is a value and a test can walk
 * any sequence of them.
 */
import {initialRoute, type Route, type RouteAction, routeReducer} from '../route.js';
import {SETTINGS_TABS, type SettingsTab} from '../settings/tabs.js';

/** The rail's destinations. */
export type Destination = 'runs' | 'settings';

export interface Navigation {
  readonly destination: Destination;
  /** Where the runs destination is: its list, or one run. Kept while another destination shows. */
  readonly runs: Route;
  /** The tab the settings destination shows. Kept while another destination shows. */
  readonly settingsTab: SettingsTab;
}

export type NavigationAction =
  | RouteAction
  /** The rail's Runs: the runs destination, at its list when it was already showing. */
  | {readonly type: 'open-runs'}
  | {readonly type: 'open-settings'}
  | {readonly type: 'pick-settings-tab'; readonly tab: SettingsTab}
  /** Escape or Alt+Left: one step back, never out of the app. */
  | {readonly type: 'back'};

export type NavigationKind = NavigationAction['type'];

/** Every kind of `NavigationAction`; the type makes a kind added to the union a compile error here. */
const KINDS = {
  'open-run': true,
  'close-run': true,
  show: true,
  'pick-agent': true,
  'close-agent': true,
  'open-runs': true,
  'open-settings': true,
  'pick-settings-tab': true,
  back: true,
} as const satisfies Record<NavigationKind, true>;

/** Each kind of `NavigationAction`, so a test can hold every one to a control that sends it. */
export const NAVIGATION_KINDS = Object.keys(KINDS) as readonly NavigationKind[];

export const initialNavigation: Navigation = {
  destination: 'runs',
  runs: initialRoute,
  settingsTab: SETTINGS_TABS[0],
};

/** One step back inside the runs: the agent first, then the run. */
function stepBack(route: Route): Route {
  if (route.screen === 'runs') return route;
  return routeReducer(route, route.agentId === null ? {type: 'close-run'} : {type: 'close-agent'});
}

export function navigationReducer(navigation: Navigation, action: NavigationAction): Navigation {
  switch (action.type) {
    case 'open-run':
    case 'close-run':
    case 'show':
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
    case 'open-settings':
      return {...navigation, destination: 'settings'};
    case 'pick-settings-tab':
      return navigation.destination === 'settings'
        ? {...navigation, settingsTab: action.tab}
        : navigation;
    case 'back':
      return navigation.destination === 'settings'
        ? {...navigation, destination: 'runs'}
        : {...navigation, runs: stepBack(navigation.runs)};
  }
}
