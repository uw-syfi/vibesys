/**
 * Everything the app holds about one host: its connection, the folded model, the streams (the runs,
 * one agent's, one run's cost) and the commands. Built once per host in a `Scope`; nothing dials
 * until a screen mounts a view or submits a command.
 */

import * as Effect from 'effect/Effect';
import type * as Atom from 'effect/reactivity/Atom';
import type * as Scope from 'effect/Scope';
import type {HostKey, HostModel} from '../state/index.js';
import {type AgentHistory, makeAgentHistory} from './agent-history.js';
import {type AgentView, makeAgentView} from './agent-view.js';
import {type CatchUp, makeCatchUp} from './catch-up.js';
import {type Commands, makeCommands} from './commands.js';
import {type HostConnectionApi, makeHostConnection} from './connection.js';
import {type CostFeed, makeCostFeed} from './cost-feed.js';
import {makeHostReadiness} from './host-control.js';
import {makeHostStore} from './host-store.js';
import type {Ids} from './ids.js';
import {makeOrchestratorClient} from './orchestrator-client.js';
import {defaultReconnect} from './reconnect.js';
import {makeRunHistory, type RunHistory} from './run-history.js';
import {makeRunsView, type RunsView} from './runs-view.js';
import type {GatewayTransport} from './transport/gateway-transport.js';

export interface HostSession {
  readonly connection: HostConnectionApi;
  /** Everything folded from the streams so far; read it through the views in `state`. */
  readonly model: Atom.Atom<HostModel>;
  readonly runs: RunsView;
  readonly agents: AgentView;
  /** Reads of an agent's past: earlier pages and whole items. */
  readonly history: AgentHistory;
  /** Reads of a run's history, which a snapshot of runs does not hold. */
  readonly runHistory: RunHistory;
  readonly costs: CostFeed;
  readonly commands: Commands;
  /** The press of Catch me up, over `commands`. */
  readonly catchUp: CatchUp;
}

export const makeHostSession = Effect.fn('HostSession.make')(function* (
  host: HostKey,
): Effect.fn.Return<HostSession, never, GatewayTransport | Ids | Scope.Scope> {
  const client = yield* makeOrchestratorClient(host);
  const readiness = yield* makeHostReadiness(host);
  const connection = yield* makeHostConnection(client, defaultReconnect, readiness);
  const store = yield* makeHostStore;
  const ambient = yield* Effect.context<never>();
  const parts = {client, connection, store, ambient, blocked: readiness.blocked};
  const commands = yield* makeCommands(parts);
  return {
    connection,
    model: store.atom,
    runs: yield* makeRunsView(parts),
    agents: yield* makeAgentView(parts),
    history: yield* makeAgentHistory(parts),
    runHistory: yield* makeRunHistory(parts),
    costs: yield* makeCostFeed(parts),
    commands,
    catchUp: yield* makeCatchUp(commands),
  };
});
