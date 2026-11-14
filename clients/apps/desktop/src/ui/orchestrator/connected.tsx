/**
 * The orchestrator window bound to the app's services. Each host's session supplies the atoms; this
 * mounts the streams the open screen needs (the run stream always, an agent's while it is open),
 * hands the screens plain snapshots, and turns their callbacks into commands.
 */

import {useAtomMount, useAtomValue} from '@effect/atom-react';
import {Page, Text} from '@vibesys/ui';
import type * as Effect from 'effect/Effect';
import * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import {type JSX, useMemo} from 'react';
import type {HistoryStatus, HostSession, Intent, RunHistoryStatus} from '../../core/index.js';
import type {
  CommandProgress,
  CostFeedSnapshot,
  CostTarget,
  FeedStatus,
  FleetPlace,
  NavigationAction,
  Route,
} from '../../state/index.js';
import {costTarget, followedAgent, isManagedRun} from '../../state/index.js';
import {type AppCommands, type HostSnapshot, OrchestratorApp} from './app.js';
import {nowMs} from './clock.js';
import {
  beginSetupIntent,
  completeSetupIntent,
  sendIntent,
  startIntent,
  stopAgentIntent,
  stopIntent,
  updateRunIntent,
} from './intents.js';
import type {RunUpdateRequest} from './run-settings/update-draft.js';
import type {CompleteSetupRequest} from './setup/index.js';
import type {StartRunRequest} from './start-draft.js';

/** One host the window reads, by the `HostKey` its routes name. */
export interface ConnectedHost {
  readonly key: string;
  readonly label: string;
  readonly session: HostSession;
}

/** Runs `effect` and hands its answer to `then`; the entry owns where it runs. */
export type Dispatch = <A>(effect: Effect.Effect<A>, then: (value: A) => void) => void;

/** Where a command went: the host's session and the id it was issued under. */
interface Issued {
  readonly host: string;
  readonly id: string;
}

/** The commands this window has issued, kept above the screens so leaving them forgets none. */
export interface Issues {
  readonly start: Issued | undefined;
  readonly stops: Readonly<Record<string, Issued>>;
  readonly stopAgents: Readonly<Record<string, Issued>>;
  readonly sends: Readonly<Record<string, Issued>>;
  readonly updates: Readonly<Record<string, Issued>>;
  readonly completes: Readonly<Record<string, Issued>>;
}

export const NO_ISSUES: Issues = {
  start: undefined,
  stops: {},
  stopAgents: {},
  sends: {},
  updates: {},
  completes: {},
};

const NO_FOLDERS: Readonly<Record<string, string>> = {};
const OPENING: FeedStatus = {_tag: 'Opening'};
const IDLE: HistoryStatus = {_tag: 'Idle'};
const RUN_HISTORY_IDLE: RunHistoryStatus = {_tag: 'Idle'};

/**
 * One agent's stream and one run's history on their host, followed while this is mounted. The history
 * of a run is read, and read again after a new snapshot forgot it, for as long as a screen of the run
 * is open.
 */
const watchOpen = (
  hosts: readonly ConnectedHost[],
  host: string | undefined,
  agentId: string | null | undefined,
  runId: string | undefined,
): Atom.Atom<void> =>
  Atom.make(get => {
    const found = hosts.find(candidate => candidate.key === host);
    if (found === undefined) return;
    const state = get(found.session.model).data;
    if (!isManagedRun(state, runId)) return;
    // A run in setup is its lead's conversation: that agent is followed with no route naming it.
    const followed = followedAgent(state, runId, agentId);
    if (followed !== null) get(found.session.agents.watch(followed));
    get(found.session.runHistory.watch(runId));
  });

/** Whole-history search while Runs is open; each session owns its refresh and stale-answer rules. */
const watchRunSearch = (
  hosts: readonly ConnectedHost[],
  query: string,
  selected: string | null,
  open: boolean,
): Atom.Atom<void> =>
  Atom.make(get => {
    if (!open) return;
    for (const host of hosts) {
      if (query.trim() === '' || selected === null || selected === host.key)
        get(host.session.runSearch.watch(query));
    }
  });

/** One run's cost stream on its host, followed while this is mounted. */
const watchCost = (
  hosts: readonly ConnectedHost[],
  host: string | undefined,
  runId: string | undefined,
): Atom.Atom<void> =>
  Atom.make(get => {
    const found = hosts.find(candidate => candidate.key === host);
    if (found !== undefined && isManagedRun(get(found.session.model).data, runId))
      get(found.session.costs.watch(runId));
  });

/** How the atom below reads another atom. */
type Reader = <A>(atom: Atom.Atom<A>) => A;

/** Where the read of the open run's history stands on `session`; idle when no run of it is open. */
const openRunHistory = (
  get: Reader,
  session: HostSession,
  runId: string | undefined,
): RunHistoryStatus =>
  runId === undefined ? RUN_HISTORY_IDLE : get(session.runHistory.status(runId));

/** Where the reads of the open agent's past stand on `session`; idle when no agent of it is open. */
const openAgentHistory = (
  get: Reader,
  session: HostSession,
  agentId: string | null | undefined,
): HostSnapshot['history'] =>
  agentId === null || agentId === undefined
    ? {earlier: IDLE, wholes: {}}
    : {
        earlier: get(session.history.earlier(agentId)),
        wholes: get(session.history.wholes(agentId)),
      };

/** What the route has open, as the snapshot of each host reads it. */
interface OpenIn {
  readonly openHost: string | undefined;
  readonly openAgent: string | null | undefined;
  readonly openRun: string | undefined;
  readonly cost: CostTarget | null;
  /** The repositories folder of each host, by host key. */
  readonly repositories: Readonly<Record<string, string>>;
}

/** One host as the screens read it: its model and connection, and the streams of what is open on it. */
const snapshotOfHost = (
  get: Reader,
  {key, label, session}: ConnectedHost,
  open: OpenIn,
): HostSnapshot => {
  const model = get(session.model);
  const here = open.openHost === key;
  const managedRun = here && isManagedRun(model.data, open.openRun) ? open.openRun : undefined;
  // A run in setup follows its lead as well, which no route names.
  const agent =
    managedRun === undefined ? null : followedAgent(model.data, managedRun, open.openAgent);
  const {cost} = open;
  const managedCost = cost?.host === key && isManagedRun(model.data, cost.runId) ? cost : null;
  return {
    key,
    label,
    model,
    connection: get(session.connection.state),
    freshness: get(session.connection.freshness),
    runsFeed: get(session.runs.status),
    runIndex: get(session.runIndex.index),
    runsRefresh: get(session.runs.refreshState),
    runSearch: get(session.runSearch.state),
    repositoryChecked: get(session.repositoryCheck.state),
    directories: get(session.directories.state),
    catalogue: get(session.newRun.catalogue),
    evalPlaces: get(session.newRun.places),
    repositoriesDirectory: open.repositories[key] ?? '',
    provision: get(session.newRun.provision),
    agentFeeds: agent === null ? {} : {[agent]: get(session.agents.status(agent)) ?? OPENING},
    costFeeds:
      managedCost !== null
        ? {
            [managedCost.runId]: {
              status: get(session.costs.status(managedCost.runId)),
              freshness: get(session.costs.freshness(managedCost.runId)),
            } satisfies CostFeedSnapshot,
          }
        : {},
    runHistory: openRunHistory(get, session, managedRun),
    history: openAgentHistory(get, session, agent),
  };
};

/** Everything the screens show, read from the sessions. */
const snapshots = (
  hosts: readonly ConnectedHost[],
  issues: Issues,
  open: {
    readonly host: string | undefined;
    readonly agent: string | null | undefined;
    readonly run: string | undefined;
  },
  cost: CostTarget | null,
  repositories: Readonly<Record<string, string>>,
): Atom.Atom<{
  readonly hosts: readonly HostSnapshot[];
  readonly progress: AppCommandsProgress;
}> =>
  Atom.make(get => {
    const {host: openHost, agent: openAgent, run: openRun} = open;
    const progressOf = (issued: Issued | undefined): CommandProgress | undefined => {
      const session = hosts.find(candidate => candidate.key === issued?.host)?.session;
      return issued === undefined || session === undefined
        ? undefined
        : get(session.commands.progress(issued.id));
    };
    const each = (issued: Readonly<Record<string, Issued>>) =>
      Object.fromEntries(
        Object.entries(issued).flatMap(([name, one]) => {
          const progress = progressOf(one);
          return progress === undefined ? [] : [[name, progress] as const];
        }),
      );
    return {
      hosts: hosts.map(host =>
        snapshotOfHost(get, host, {openHost, openAgent, openRun, cost, repositories}),
      ),
      progress: {
        start: progressOf(issues.start),
        stop: each(issues.stops),
        agentStop: each(issues.stopAgents),
        send: each(issues.sends),
        update: each(issues.updates),
        complete: each(issues.completes),
      },
    };
  });

type AppCommandsProgress = Pick<
  AppCommands,
  'start' | 'stop' | 'agentStop' | 'send' | 'update' | 'complete'
>;

/** What the route has open: the host, the agent and the run, and the run whose cost stream is followed. */
function openOf(route: Route) {
  const cost = costTarget(route);
  return {
    openHost: route.screen === 'run' ? route.host : undefined,
    openAgent: route.screen === 'run' ? route.agentId : undefined,
    openRun: route.screen === 'run' ? route.runId : undefined,
    costHost: cost?.host,
    costRun: cost?.runId,
  };
}

/** The commands of the New run screen that read the host's catalogue and make a repository from an example. */
function newRunCommands(
  sessionOf: (host: string) => HostSession | undefined,
  dispatch: Dispatch,
  setIssues: (update: (all: Issues) => Issues) => void,
): Pick<
  AppCommands,
  'loadNewRun' | 'startFromExample' | 'setUpInNewRepository' | 'forgetProvision' | 'clearStart'
> {
  return {
    loadNewRun: host => {
      const session = sessionOf(host);
      if (session !== undefined) dispatch(session.newRun.load, () => undefined);
    },
    startFromExample: (host, example, request) => {
      const session = sessionOf(host);
      if (session === undefined) return;
      dispatch(
        session.newRun.startFromExample(example, repository =>
          startIntent({...request, repository}),
        ),
        id => {
          if (id !== undefined) setIssues(all => ({...all, start: {host, id}}));
        },
      );
    },
    setUpInNewRepository: (host, repository, request) => {
      const session = sessionOf(host);
      if (session === undefined) return;
      dispatch(
        session.newRun.startInNewRepository(repository, made =>
          beginSetupIntent({...request, repository: made}),
        ),
        id => {
          if (id !== undefined) setIssues(all => ({...all, start: {host, id}}));
        },
      );
    },
    clearStart: () => setIssues(all => ({...all, start: undefined})),
    forgetProvision: host => {
      const session = sessionOf(host);
      if (session !== undefined) dispatch(session.newRun.forgetProvision, () => undefined);
    },
  };
}

/** Try again everything of `session` that may be down: the connection, the runs, and what is open. */
function retryFeeds(
  dispatch: Dispatch,
  session: HostSession,
  agentId: string | null | undefined,
  runId: string | undefined,
): void {
  dispatch(session.connection.retry, () => undefined);
  dispatch(session.runs.retry, () => undefined);
  if (agentId !== null && agentId !== undefined)
    dispatch(session.agents.retry(agentId), () => undefined);
  if (runId !== undefined) dispatch(session.runHistory.retry(runId), () => undefined);
}

/**
 * The streams the open screen needs, followed while it is open: the open agent's stream and the open
 * run's history, which the run stream does not carry, and the open run's cost stream, which is open
 * only while the Cost view shows (leaving it closes the stream).
 */
function useOpenStreams(hosts: readonly ConnectedHost[], open: ReturnType<typeof openOf>): void {
  const {openHost, openAgent, openRun, costHost, costRun} = open;
  useAtomMount(
    useMemo(
      () => watchOpen(hosts, openHost, openAgent, openRun),
      [hosts, openHost, openAgent, openRun],
    ),
  );
  useAtomMount(useMemo(() => watchCost(hosts, costHost, costRun), [hosts, costHost, costRun]));
}

function retryHostFeeds(
  dispatch: Dispatch,
  session: HostSession,
  snapshot: HostSnapshot | undefined,
  open: boolean,
  agentId: string | null | undefined,
  runId: string | undefined,
): void {
  const managedRun =
    open && snapshot !== undefined && isManagedRun(snapshot.model.data, runId) ? runId : undefined;
  retryFeeds(dispatch, session, managedRun === undefined ? undefined : agentId, managedRun);
}

/** Mount the route-owned reads and return their current folded snapshot. */
function useConnectedState(
  hosts: readonly ConnectedHost[],
  issues: Issues,
  route: Route,
  fleet: FleetPlace,
  repositories: Readonly<Record<string, string>>,
) {
  const open = openOf(route);
  const {openHost, openAgent, openRun, costHost, costRun} = open;
  useOpenStreams(hosts, open);
  useAtomMount(
    useMemo(
      () => watchRunSearch(hosts, fleet.query ?? '', fleet.host, route.screen === 'runs'),
      [hosts, fleet.query, fleet.host, route.screen],
    ),
  );
  const view = useAtomValue(
    useMemo(
      () =>
        snapshots(
          hosts,
          issues,
          {host: openHost, agent: openAgent, run: openRun},
          costHost === undefined || costRun === undefined ? null : {host: costHost, runId: costRun},
          repositories,
        ),
      [hosts, issues, openHost, openAgent, openRun, costHost, costRun, repositories],
    ),
  );
  return {open, view};
}

/** The commands that change a live run's settings, whose last outcome is kept by run id. */
function updateCommands(
  issue: (host: string, intent: Intent, kept: (issued: Issued) => void) => void,
  retry: (issued: Issued | undefined) => void,
  issues: Issues,
  setIssues: (update: (all: Issues) => Issues) => void,
): Pick<AppCommands, 'updateRun' | 'retryUpdate'> {
  return {
    updateRun: (host: string, runId: string, request: RunUpdateRequest) =>
      issue(host, updateRunIntent(runId, request), update =>
        setIssues(all => ({...all, updates: {...all.updates, [runId]: update}})),
      ),
    retryUpdate: runId => retry(issues.updates[runId]),
  };
}

/** The commands of a run's setup: begin one, and complete it into an optimizing run. */
function setupCommands(
  issue: (host: string, intent: Intent, kept: (issued: Issued) => void) => void,
  retry: (issued: Issued | undefined) => void,
  issues: Issues,
  setIssues: (update: (all: Issues) => Issues) => void,
): Pick<AppCommands, 'beginSetup' | 'completeSetup' | 'retryComplete'> {
  return {
    // A begun setup is answered where a start is: the New run screen shows it and opens its run.
    beginSetup: (host: string, request: StartRunRequest) =>
      issue(host, beginSetupIntent(request), start => setIssues(all => ({...all, start}))),
    completeSetup: (host: string, runId: string, request: CompleteSetupRequest) =>
      issue(host, completeSetupIntent(runId, request), done =>
        setIssues(all => ({...all, completes: {...all.completes, [runId]: done}})),
      ),
    retryComplete: runId => retry(issues.completes[runId]),
  };
}

/**
 * The runs of every host, one open at a time. The run streams are followed by the window (the
 * shell mounts them), and where the person is comes from the shell's navigation.
 */
interface ConnectedAppProps {
  readonly hosts: readonly ConnectedHost[];
  readonly dispatch: Dispatch;
  readonly route: Route;
  readonly fleet: FleetPlace;
  readonly go: (action: NavigationAction) => void;
  readonly issues: Issues;
  readonly setIssues: (update: (all: Issues) => Issues) => void;
  /** The Repositories folder kept for each machine, by host key: New run's Copy to starts there. */
  readonly repositories?: Readonly<Record<string, string>>;
}

export function ConnectedApp(props: ConnectedAppProps): JSX.Element {
  const {hosts, dispatch, route, fleet, go, issues, setIssues} = props;
  const repositories = props.repositories ?? NO_FOLDERS;
  const {open, view} = useConnectedState(hosts, issues, route, fleet, repositories);
  const {openHost, openAgent, openRun} = open;
  const clock = useAtomValue(nowMs);
  const sessionOf = (host: string): HostSession | undefined =>
    hosts.find(candidate => candidate.key === host)?.session;
  /** Runs what `act` makes on the host's session, if the window has one for it. */
  const on = (host: string, act: (session: HostSession) => Effect.Effect<void>): void => {
    const session = sessionOf(host);
    if (session !== undefined) dispatch(act(session), () => undefined);
  };
  const issue = (host: string, intent: Intent, kept: (issued: Issued) => void): void => {
    const session = sessionOf(host);
    if (session !== undefined) dispatch(session.commands.submit(intent), id => kept({host, id}));
  };
  const retry = (issued: Issued | undefined): void => {
    const session = issued === undefined ? undefined : sessionOf(issued.host);
    if (issued !== undefined && session !== undefined)
      dispatch(session.commands.retry(issued.id), () => undefined);
  };
  const commands: AppCommands = {
    ...view.progress,
    startRun: (host: string, request: StartRunRequest) =>
      issue(host, startIntent(request), start => setIssues(all => ({...all, start}))),
    checkRepository: (host, repository) =>
      on(host, session => session.repositoryCheck.ask(repository)),
    listDirectory: (host, folder, hidden, fresh) =>
      on(host, session => session.directories.want(folder, hidden, fresh)),
    ...newRunCommands(sessionOf, dispatch, setIssues),
    retryStart: () => retry(issues.start),
    refreshRuns: host => on(host, session => session.runs.refresh),
    stopRun: (host, runId) =>
      issue(host, stopIntent(runId), stop =>
        setIssues(all => ({...all, stops: {...all.stops, [runId]: stop}})),
      ),
    retryStop: runId => retry(issues.stops[runId]),
    stopAgent: (host, agentId) =>
      issue(host, stopAgentIntent(agentId), stop =>
        setIssues(all => ({...all, stopAgents: {...all.stopAgents, [agentId]: stop}})),
      ),
    retryStopAgent: agentId => retry(issues.stopAgents[agentId]),
    sendMessage: (host, agentId, text) =>
      issue(host, sendIntent(agentId, text), send =>
        setIssues(all => ({...all, sends: {...all.sends, [agentId]: send}})),
      ),
    retrySend: agentId => retry(issues.sends[agentId]),
    ...updateCommands(issue, retry, issues, setIssues),
    ...setupCommands(issue, retry, issues, setIssues),
    loadEarlier: (host, agentId, before) =>
      on(host, session => session.history.loadEarlier(agentId, before)),
    expand: (host, agentId, ordinal) =>
      on(host, session => session.history.expand(agentId, ordinal)),
  };
  if (!AsyncResult.isSuccess(clock))
    return (
      <Page state="loading" header={<Text variant="title">Orchestrated runs</Text>}>
        <Text tone="muted">Starting.</Text>
      </Page>
    );
  return (
    <OrchestratorApp
      hosts={view.hosts}
      nowMs={clock.value}
      commands={commands}
      route={route}
      fleet={fleet}
      go={go}
      retryCost={(host, runId) => on(host, session => session.costs.retry(runId))}
      retryHost={host => {
        const session = sessionOf(host);
        if (session === undefined) return;
        const snapshot = view.hosts.find(candidate => candidate.key === host);
        retryHostFeeds(dispatch, session, snapshot, openHost === host, openAgent, openRun);
      }}
    />
  );
}
