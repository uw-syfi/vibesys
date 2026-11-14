/**
 * Everything the app holds about one host: its connection, the folded model, the streams (the runs,
 * one agent's, one run's cost, the host's machine stats) and the commands. Built once per host in a
 * `Scope`; nothing dials until a screen mounts a view or submits a command.
 */

import {HostService} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Atom from 'effect/reactivity/Atom';
import type * as Scope from 'effect/Scope';
import type {HostKey, HostModel} from '../state/index.js';
import {type AgentHistory, makeAgentHistory} from './agent-history.js';
import {type AgentView, makeAgentView} from './agent-view.js';
import {type CatchUp, makeCatchUp} from './catch-up.js';
import {makeClient} from './client/make-client.js';
import {type Commands, makeCommands} from './commands.js';
import {type HostConnectionApi, makeHostConnection} from './connection.js';
import {type CostFeed, makeCostFeed} from './cost-feed.js';
import {type DirectoryListings, makeDirectoryListings} from './directory-listing.js';
import {makeHostReadiness} from './host-control.js';
import {type HostCostFeed, makeHostCostFeed} from './host-cost-feed.js';
import {makeHostStore} from './host-store.js';
import type {Ids} from './ids.js';
import {type MachineStatsFeed, makeMachineStatsFeed} from './machine-stats-feed.js';
import {makeNewRun, type NewRun} from './new-run.js';
import {makeOrchestratorClient} from './orchestrator-client.js';
import {defaultReconnect} from './reconnect.js';
import {makeRepositoryCheck, type RepositoryCheck} from './repository-check.js';
import {makeRunDetailFeed, type RunDetailFeed} from './run-detail/index.js';
import {makeRunHistory, type RunHistory} from './run-history.js';
import {makeRunSearch, type RunSearch} from './run-search.js';
import {makeRunsTopicFeed, type RunsTopicFeed} from './runs-topic-feed.js';
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
  /** Search of every durable run, including old finished runs omitted from the bounded snapshot. */
  readonly runSearch: RunSearch;
  readonly costs: CostFeed;
  /** What every run of the host has cost, in the window a screen asks for. */
  readonly hostCosts: HostCostFeed;
  /** The CPU, memory and GPUs of the machine the host runs on. */
  readonly machine: MachineStatsFeed;
  /** The host gateway's latest-value run index. Managed orchestrator runs remain in `runs`. */
  readonly runIndex: RunsTopicFeed;
  /** One generic run's typed Skeleton/Tail projection, opened only while that run is mounted. */
  readonly runDetail: RunDetailFeed;
  /** Both disjoint run-discovery streams, mounted for exactly the run navigator's lifetime. */
  readonly runNavigation: Atom.Atom<void>;
  readonly commands: Commands;
  /** The host's answer about the repository path being typed in the Start form. */
  readonly repositoryCheck: RepositoryCheck;
  /** The host's folder listings, for completing the repository path as it is typed. */
  readonly directories: DirectoryListings;
  /** The press of Catch me up, over `commands`. */
  readonly catchUp: CatchUp;
  /** What the New run screen asks of the host: its catalogue, eval places, and a repository from an example. */
  readonly newRun: NewRun;
}

export const makeHostSession = Effect.fn('HostSession.make')(function* (
  host: HostKey,
): Effect.fn.Return<HostSession, never, GatewayTransport | Ids | Scope.Scope> {
  const client = yield* makeOrchestratorClient(host);
  const readiness = yield* makeHostReadiness(host);
  const connection = yield* makeHostConnection(client, defaultReconnect, readiness);
  const store = yield* makeHostStore;
  const ambient = yield* Effect.context<never>();
  const parts = {client, connection, store, ambient};
  const commands = yield* makeCommands(parts);
  const directories = yield* makeDirectoryListings(parts);
  const hostClient = yield* makeClient(HostService, host);
  const runs = yield* makeRunsView(parts);
  const runIndex = yield* makeRunsTopicFeed({
    client: hostClient,
    connection,
    ambient,
  });
  const runNavigation = Atom.make(get => {
    get(runs.watch);
    get(runIndex.watch);
  });
  return {
    connection,
    model: store.atom,
    runs,
    agents: yield* makeAgentView(parts),
    history: yield* makeAgentHistory(parts),
    runHistory: yield* makeRunHistory(parts),
    runSearch: yield* makeRunSearch(parts),
    costs: yield* makeCostFeed(parts),
    hostCosts: yield* makeHostCostFeed(parts),
    machine: yield* makeMachineStatsFeed({
      client: hostClient,
      connection,
      ambient,
    }),
    runIndex,
    runDetail: yield* makeRunDetailFeed({
      client: hostClient,
      connection,
      ambient,
    }),
    runNavigation,
    commands,
    repositoryCheck: yield* makeRepositoryCheck(parts),
    directories,
    catchUp: yield* makeCatchUp(commands),
    newRun: yield* makeNewRun(parts, commands, directories),
  };
});
