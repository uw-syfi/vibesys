/**
 * Where the Run screen is zoomed: the run, one idea (a candidate) or one agent, one step under it
 * (an agent turn or a gate), or one item of that step (a tool call). The route is part of the
 * window's location, as `routeQuery` writes it and `routeFromQuery` reads it, so an entry of the
 * window's history restores it; the screen keeps no route of its own.
 *
 * Every zoom the user makes (`ZoomEvent`, a crumb's `jump` among them) is a step of the route
 * (`zoomStep`), which the page sends to the location as one new entry.
 * The mode (`ideas`, `timeline`, `agents`, `usage`, `logs`) switches what the panes show and keeps the
 * level, except that Timeline, Agents and Usage list the whole run, so choosing one closes an open idea
 * or agent (an idea is only ever open in the Ideas or Logs mode). `up` goes one level up: from an item to its step, a step to its idea or agent, an idea or
 * agent to the run, and at the run back to the Ideas.
 */

/** What the run level's left pane shows, and at deeper levels whether the detail shows its logs. */
export type RunMode = 'ideas' | 'timeline' | 'agents' | 'usage' | 'logs';
export const RUN_MODES: readonly RunMode[] = ['ideas', 'timeline', 'agents', 'usage', 'logs'];

/** The zoom as the address holds it. Ids that are not in the run resolve a level up. */
export interface Route {
  readonly mode: RunMode;
  /** The candidate opened (its id), or null. */
  readonly idea: string | null;
  /** The agent opened from the Agents face, or null; an idea and an agent are never both open. */
  readonly agent: string | null;
  /** The step's span under the idea or agent, or null. */
  readonly step: string | null;
  /** The item of the step (a tool call's transcript entry), or null. */
  readonly item: string | null;
}

export const RUN_ROUTE: Route = {mode: 'ideas', idea: null, agent: null, step: null, item: null};

export type ZoomEvent =
  | {readonly type: 'open-idea'; readonly idea: string}
  | {readonly type: 'open-agent'; readonly agent: string}
  /** Open a step: under `idea` (a shortcut from the run level), else under what is open. */
  | {readonly type: 'open-step'; readonly step: string; readonly idea?: string}
  | {readonly type: 'open-item'; readonly item: string}
  | {readonly type: 'mode'; readonly mode: RunMode}
  | {readonly type: 'up'}
  /** Go straight to a route the screen names: a crumb, or a banner's shortcut. */
  | {readonly type: 'jump'; readonly route: Route};

/** How deep a route goes. */
export type ZoomLevel = 'run' | 'opened' | 'step' | 'item';

export function routeLevel(route: Route): ZoomLevel {
  if ((route.idea !== null || route.agent !== null) && route.step !== null) {
    return route.item === null ? 'step' : 'item';
  }
  return route.idea !== null || route.agent !== null ? 'opened' : 'run';
}

/** The route one level up from `route`. */
export function routeUp(route: Route): Route {
  switch (routeLevel(route)) {
    case 'item':
      return {...route, item: null};
    case 'step':
      return {...route, step: null, item: null};
    case 'opened':
      return {...RUN_ROUTE, mode: route.agent === null ? 'ideas' : 'agents'};
    case 'run':
      return RUN_ROUTE;
  }
}

/** Whether the face covers the whole run, which an open idea or agent would hide. */
function listsRun(mode: RunMode): boolean {
  return mode === 'timeline' || mode === 'agents' || mode === 'usage';
}

function isOpen(route: Route): boolean {
  return route.idea !== null || route.agent !== null;
}

/** At a deeper level the mode only says whether the detail shows its logs. */
function keptMode(mode: string | undefined): RunMode {
  return mode === 'logs' ? 'logs' : 'ideas';
}

/** The route `event` zooms `route` to; `route` itself when the event means nothing there. */
export function zoomStep(route: Route, event: ZoomEvent): Route {
  switch (event.type) {
    case 'open-idea':
      return {...RUN_ROUTE, mode: keptMode(route.mode), idea: event.idea};
    case 'open-agent':
      return {...RUN_ROUTE, mode: 'agents', agent: event.agent};
    case 'open-step':
      if (event.idea === undefined && routeLevel(route) === 'run') return route;
      return {
        ...route,
        mode: event.idea === undefined ? route.mode : keptMode(route.mode),
        idea: event.idea ?? route.idea,
        agent: event.idea === undefined ? route.agent : null,
        step: event.step,
        item: null,
      };
    case 'open-item':
      return route.step === null ? route : {...route, item: event.item};
    case 'mode':
      // The Timeline, Agents and Usage faces list the whole run, so they replace an open idea or agent.
      return listsRun(event.mode) && isOpen(route)
        ? {...RUN_ROUTE, mode: event.mode}
        : {...route, mode: event.mode};
    case 'up':
      return routeUp(route);
    case 'jump':
      return event.route;
  }
}

const KEYS = ['idea', 'agent', 'step', 'item'] as const;

/** The route as the address's fragment query: `mode=logs&idea=…`; the run's own route is empty. */
export function routeQuery(route: Route): string {
  const pairs: string[] = [];
  if (route.mode !== 'ideas') pairs.push(`mode=${route.mode}`);
  for (const key of KEYS) {
    const value = route[key];
    if (value !== null) pairs.push(`${key}=${encodeURIComponent(value)}`);
  }
  return pairs.join('&');
}

function queryValues(text: string): ReadonlyMap<string, string> {
  const values = new Map<string, string>();
  for (const pair of (text.startsWith('#') ? text.slice(1) : text).split('&')) {
    const at = pair.indexOf('=');
    if (at <= 0) continue;
    try {
      values.set(pair.slice(0, at), decodeURIComponent(pair.slice(at + 1)));
    } catch {
      // A malformed escape names nothing: the route falls back as if the key were absent.
    }
  }
  return values;
}

/** The route a fragment query names; anything it does not name is the run's. */
export function routeFromQuery(text: string): Route {
  const query = queryValues(text);
  const mode = query.get('mode');
  const idea = query.get('idea') ?? null;
  const agent = idea === null ? (query.get('agent') ?? null) : null;
  const opened = idea !== null || agent !== null;
  const step = opened ? (query.get('step') ?? null) : null;
  return {
    mode: idea === null ? (RUN_MODES.find(each => each === mode) ?? 'ideas') : keptMode(mode),
    idea,
    agent,
    step,
    item: step === null ? null : (query.get('item') ?? null),
  };
}
