/**
 * Where the Run screen is zoomed: the run, one idea (a candidate) or one agent, one step under it
 * (an agent turn or a gate), or one item of that step (a tool call). The zoom is the screen's
 * control state, so it is an XState machine (`zoomMachine`) with a state per level; its context is
 * the route, which lives in the window's address (`routeQuery`, `routeFromQuery`) so browser Back
 * walks the zoom back.
 *
 * Every zoom the user makes runs the `remember` action, which the page provides to push the route
 * into the window's history; `restore` (Back, Forward, or a crumb) sets a route without pushing.
 * The mode (`ideas`, `timeline`, `agents`, `logs`) switches what the panes show and keeps the
 * level. `up` goes one level up: from an item to its step, a step to its idea or agent, an idea or
 * agent to the run, and at the run back to the Ideas.
 */
import {assign, setup} from 'xstate';

/** What the run level's left pane shows, and at deeper levels whether the detail shows its logs. */
export type RunMode = 'ideas' | 'timeline' | 'agents' | 'logs';
export const RUN_MODES: readonly RunMode[] = ['ideas', 'timeline', 'agents', 'logs'];

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
  /** Set the route without remembering it: Back, Forward, a crumb that names a route. */
  | {readonly type: 'restore'; readonly route: Route};

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

const fromRestore = (level: ZoomLevel) => ({
  guard: ({event}: {event: ZoomEvent}) =>
    event.type === 'restore' && routeLevel(event.route) === level,
});

export const zoomMachine = setup({
  types: {context: {} as {route: Route}, events: {} as ZoomEvent},
  actions: {
    /** Push the route into the window's history; the page provides it. */
    remember: () => undefined,
    restoreRoute: assign(({event}) => (event.type === 'restore' ? {route: event.route} : {})),
    goUp: assign(({context}) => ({route: routeUp(context.route)})),
    openStep: assign(({context, event}) =>
      event.type === 'open-step'
        ? {
            route: {
              ...context.route,
              idea: event.idea ?? context.route.idea,
              agent: event.idea === undefined ? context.route.agent : null,
              step: event.step,
              item: null,
            },
          }
        : {},
    ),
  },
}).createMachine({
  id: 'zoom',
  context: {route: RUN_ROUTE},
  initial: 'run',
  on: {
    'open-idea': {
      target: '.opened',
      actions: [
        assign(({context, event}) => ({
          route: {...RUN_ROUTE, mode: keptMode(context.route.mode), idea: event.idea},
        })),
        'remember',
      ],
    },
    'open-agent': {
      target: '.opened',
      actions: [
        assign(({event}) => ({route: {...RUN_ROUTE, mode: 'agents' as const, agent: event.agent}})),
        'remember',
      ],
    },
    mode: {
      actions: [
        assign(({context, event}) => ({route: {...context.route, mode: event.mode}})),
        'remember',
      ],
    },
    restore: [
      {target: '.run', ...fromRestore('run'), actions: 'restoreRoute'},
      {target: '.opened', ...fromRestore('opened'), actions: 'restoreRoute'},
      {target: '.step', ...fromRestore('step'), actions: 'restoreRoute'},
      {target: '.item', ...fromRestore('item'), actions: 'restoreRoute'},
    ],
  },
  states: {
    run: {
      on: {
        'open-step': {
          guard: ({event}) => event.idea !== undefined,
          target: 'step',
          actions: [
            assign(({context, event}) => ({
              route: {
                ...RUN_ROUTE,
                mode: keptMode(context.route.mode),
                idea: event.idea ?? null,
                step: event.step,
              },
            })),
            'remember',
          ],
        },
        up: {
          guard: ({context}) => context.route.mode !== 'ideas',
          actions: [assign({route: RUN_ROUTE}), 'remember'],
        },
      },
    },
    opened: {
      on: {
        'open-step': {target: 'step', actions: ['openStep', 'remember']},
        up: {target: 'run', actions: ['goUp', 'remember']},
      },
    },
    step: {
      on: {
        'open-step': {target: 'step', actions: ['openStep', 'remember']},
        'open-item': {
          target: 'item',
          actions: [
            assign(({context, event}) => ({route: {...context.route, item: event.item}})),
            'remember',
          ],
        },
        up: {target: 'opened', actions: ['goUp', 'remember']},
      },
    },
    item: {
      on: {
        'open-step': {target: 'step', actions: ['openStep', 'remember']},
        'open-item': {
          actions: [
            assign(({context, event}) => ({route: {...context.route, item: event.item}})),
            'remember',
          ],
        },
        up: {target: 'step', actions: ['goUp', 'remember']},
      },
    },
  },
});

/** At a deeper level the mode only says whether the detail shows its logs. */
function keptMode(mode: RunMode): RunMode {
  return mode === 'logs' ? 'logs' : 'ideas';
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
    mode: RUN_MODES.find(each => each === mode) ?? 'ideas',
    idea,
    agent,
    step,
    item: step === null ? null : (query.get('item') ?? null),
  };
}
