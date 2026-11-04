/**
 * Everything the app holds about one host: its connection, the folded model, the two streams and
 * the commands. Built once per host in a `Scope`; nothing dials until a screen mounts a view or
 * submits a command.
 */

import * as Effect from 'effect/Effect';
import type * as Atom from 'effect/reactivity/Atom';
import type * as Scope from 'effect/Scope';
import type {HostKey, HostModel} from '../state/index.js';
import {type AgentView, makeAgentView} from './agent-view.js';
import {type Commands, makeCommands} from './commands.js';
import {type HostConnectionApi, makeHostConnection} from './connection.js';
import {makeHostStore} from './host-store.js';
import type {Ids} from './ids.js';
import {makeOrchestratorClient} from './orchestrator-client.js';
import {makeRunsView, type RunsView} from './runs-view.js';
import type {GatewayTransport} from './transport/gateway-transport.js';

export interface HostSession {
  readonly connection: HostConnectionApi;
  /** Everything folded from the streams so far; read it through the views in `state`. */
  readonly model: Atom.Atom<HostModel>;
  readonly runs: RunsView;
  readonly agents: AgentView;
  readonly commands: Commands;
}

export const makeHostSession = Effect.fn('HostSession.make')(function* (
  host: HostKey,
): Effect.fn.Return<HostSession, never, GatewayTransport | Ids | Scope.Scope> {
  const client = yield* makeOrchestratorClient(host);
  const connection = yield* makeHostConnection(client);
  const store = yield* makeHostStore;
  const ambient = yield* Effect.context<never>();
  const parts = {client, connection, store, ambient};
  return {
    connection,
    model: store.atom,
    runs: yield* makeRunsView(parts),
    agents: yield* makeAgentView(parts),
    commands: yield* makeCommands(parts),
  };
});
