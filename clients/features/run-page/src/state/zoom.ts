/**
 * Where the Run screen is zoomed: the run, one idea (a candidate) or one agent, one step under it
 * (an agent turn or a gate), or one item of that step (a tool call). The zoom is the screen's
 * control state, so it is an XState machine (`zoomMachine`) with a state per level; its context is
 * the route, which lives in the window's address (`routeQuery`, `routeFromQuery`) so browser Back
 * walks the zoom back.
 *
 * Every zoom the user makes (`ZoomEvent`, a crumb's `jump` among them) runs the `remember` action,
 * which the page provides to push the route into the window's history, so the address always names
 * the route shown. Only the window itself sends `restore` (its address on open, Back, Forward),
 * which sets the route the address already holds without pushing (`ZoomMachineEvent`).
 * The mode (`ideas`, `timeline`, `agents`, `usage`, `logs`) switches what the panes show and keeps the
 * level, except where the face cannot show what is open: Agents and Usage list the whole run, so
 * choosing one closes an open idea or agent, and the Timeline, a list of ideas, closes an open
 * agent. An idea is open in the Ideas, Logs or Timeline mode: opened from the Timeline it keeps the
 * Timeline beside it, and going up from it returns there. `up` goes one level up: from an item to
 * its step, a step to its idea or agent, an idea or agent to the run (its Timeline, Agents or
 * Ideas face), and at the run back to the Ideas.
 */
import {assign, setup} from 'xstate';

/**
 * What the run level's left pane shows; at deeper levels whether the detail shows its logs, and
 * whether the idea was opened from the Timeline.
 */
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

/**
 * What the machine takes: the user's zooms, and `restore`, which sets the route the window's
 * address already holds (on open, Back, Forward) without remembering it. Views get `ZoomEvent`
 * only, so nothing on the screen can move the zoom without the address following.
 */
export type ZoomMachineEvent = ZoomEvent | {readonly type: 'restore'; readonly route: Route};

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
      return runRouteOf(route);
    case 'run':
      return RUN_ROUTE;
  }
}

/** The run level an opened idea or agent goes up to: the face it was opened from. */
export function runRouteOf(route: Route): Route {
  if (route.agent !== null) return {...RUN_ROUTE, mode: 'agents'};
  return {...RUN_ROUTE, mode: route.mode === 'timeline' ? 'timeline' : 'ideas'};
}

function sameRoute(a: Route, b: Route): boolean {
  return (
    a.mode === b.mode &&
    a.idea === b.idea &&
    a.agent === b.agent &&
    a.step === b.step &&
    a.item === b.item
  );
}

/** A transition to `level` for a `restore` that names a route there. */
const fromRestore = (level: ZoomLevel) => ({
  guard: ({event}: {event: ZoomMachineEvent}) =>
    event.type === 'restore' && routeLevel(event.route) === level,
  actions: 'setRoute' as const,
});

/** A transition to `level` for a `jump` to a route there other than the one shown, remembered. */
const fromJump = (level: ZoomLevel) => ({
  guard: ({context, event}: {context: {route: Route}; event: ZoomMachineEvent}) =>
    event.type === 'jump' &&
    routeLevel(event.route) === level &&
    !sameRoute(event.route, context.route),
  actions: ['setRoute' as const, 'remember' as const],
});

export const zoomMachine = setup({
  types: {context: {} as {route: Route}, events: {} as ZoomMachineEvent},
  actions: {
    /** Push the route into the window's history; the page provides it. */
    remember: () => undefined,
    setRoute: assign(({event}) =>
      event.type === 'restore' || event.type === 'jump' ? {route: event.route} : {},
    ),
    goUp: assign(({context}) => ({route: routeUp(context.route)})),
    openStep: assign(({context, event}) =>
      event.type === 'open-step'
        ? {
            route: {
              ...context.route,
              mode: event.idea === undefined ? context.route.mode : keptMode(context.route.mode),
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
    mode: [
      {
        // A face that cannot show what is open replaces it: Agents and Usage list the whole run, and
        // the Timeline lists ideas, not agents.
        guard: ({context, event}) => hides(event.mode, context.route),
        target: '.run',
        actions: [assign(({event}) => ({route: {...RUN_ROUTE, mode: event.mode}})), 'remember'],
      },
      {
        actions: [
          assign(({context, event}) => ({route: {...context.route, mode: event.mode}})),
          'remember',
        ],
      },
    ],
    jump: [
      {target: '.run', ...fromJump('run')},
      {target: '.opened', ...fromJump('opened')},
      {target: '.step', ...fromJump('step')},
      {target: '.item', ...fromJump('item')},
    ],
    restore: [
      {target: '.run', ...fromRestore('run')},
      {target: '.opened', ...fromRestore('opened')},
      {target: '.step', ...fromRestore('step')},
      {target: '.item', ...fromRestore('item')},
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

/** Whether the face `mode` cannot show the idea or agent `route` has open. */
function hides(mode: RunMode, route: Route): boolean {
  if (mode === 'agents' || mode === 'usage') return route.idea !== null || route.agent !== null;
  return mode === 'timeline' && route.agent !== null;
}

/**
 * At a deeper level the mode says only whether the detail shows its logs, and whether the idea
 * was opened from the Timeline (which it then keeps beside it).
 */
function keptMode(mode: string | undefined): RunMode {
  return mode === 'logs' || mode === 'timeline' ? mode : 'ideas';
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
