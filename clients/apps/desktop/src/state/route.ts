/**
 * Where the user is: the run list, or one run with an agent picked and one of its views. A plain
 * reducer, so every move is a value and a test can walk any sequence of them.
 */
export type RunView = 'timeline' | 'agents' | 'cost' | 'handoffs' | 'ideas';

export type Route =
  | {readonly screen: 'runs'}
  | {
      readonly screen: 'run';
      readonly host: string;
      readonly runId: string;
      readonly view: RunView;
      /** The agent whose turns are open beside the run, or null. */
      readonly agentId: string | null;
    };

export type RouteAction =
  | {readonly type: 'open-run'; readonly host: string; readonly runId: string}
  | {readonly type: 'close-run'}
  | {readonly type: 'show'; readonly view: RunView}
  | {readonly type: 'pick-agent'; readonly agentId: string}
  | {readonly type: 'close-agent'};

export const initialRoute: Route = {screen: 'runs'};

export function routeReducer(route: Route, action: RouteAction): Route {
  switch (action.type) {
    case 'open-run':
      return {
        screen: 'run',
        host: action.host,
        runId: action.runId,
        view: 'timeline',
        agentId: null,
      };
    case 'close-run':
      return initialRoute;
    case 'show':
      return route.screen === 'run' ? {...route, view: action.view} : route;
    case 'pick-agent':
      return route.screen === 'run' ? {...route, agentId: action.agentId} : route;
    case 'close-agent':
      return route.screen === 'run' ? {...route, agentId: null} : route;
  }
}
