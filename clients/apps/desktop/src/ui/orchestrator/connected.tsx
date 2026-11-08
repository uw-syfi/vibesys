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
import {costTarget} from '../../state/index.js';
import {type AppCommands, type HostSnapshot, OrchestratorApp} from './app.js';
import {nowMs} from './clock.js';
import {sendIntent, startIntent, stopIntent} from './intents.js';
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
  readonly sends: Readonly<Record<string, Issued>>;
}

export const NO_ISSUES: Issues = {start: undefined, stops: {}, sends: {}};

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
    if (agentId !== null && agentId !== undefined) get(found.session.agents.watch(agentId));
    if (runId !== undefined) get(found.session.runHistory.watch(runId));
  });

/** One run's cost stream on its host, followed while this is mounted. */
const watchCost = (
  hosts: readonly ConnectedHost[],
  host: string | undefined,
  runId: string | undefined,
): Atom.Atom<void> =>
  Atom.make(get => {
    const found = hosts.find(candidate => candidate.key === host);
    if (found !== undefined && runId !== undefined) get(found.session.costs.watch(runId));
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

/** Everything the screens show, read from the sessions. */
const snapshots = (
  hosts: readonly ConnectedHost[],
  issues: Issues,
  openHost: string | undefined,
  openAgent: string | null | undefined,
  openRun: string | undefined,
  cost: CostTarget | null,
): Atom.Atom<{
  readonly hosts: readonly HostSnapshot[];
  readonly progress: AppCommandsProgress;
}> =>
  Atom.make(get => {
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
      hosts: hosts.map(({key, label, session}) => ({
        key,
        label,
        model: get(session.model),
        connection: get(session.connection.state),
        freshness: get(session.connection.freshness),
        runsFeed: get(session.runs.status),
        repositoryChecked: get(session.repositoryCheck.state),
        directories: get(session.directories.state),
        agentFeeds:
          openHost === key && openAgent !== null && openAgent !== undefined
            ? {[openAgent]: get(session.agents.status(openAgent)) ?? OPENING}
            : {},
        costFeeds:
          cost?.host === key
            ? {
                [cost.runId]: {
                  status: get(session.costs.status(cost.runId)),
                  freshness: get(session.costs.freshness(cost.runId)),
                } satisfies CostFeedSnapshot,
              }
            : {},
        runHistory: openRunHistory(get, session, openHost === key ? openRun : undefined),
        history: openAgentHistory(get, session, openHost === key ? openAgent : undefined),
      })),
      progress: {
        start: progressOf(issues.start),
        stop: each(issues.stops),
        send: each(issues.sends),
      },
    };
  });

type AppCommandsProgress = Pick<AppCommands, 'start' | 'stop' | 'send'>;

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
 * The runs of every host, one open at a time. The run streams are followed by the window (the
 * shell mounts them), and where the person is comes from the shell's navigation.
 */
export function ConnectedApp(props: {
  readonly hosts: readonly ConnectedHost[];
  readonly dispatch: Dispatch;
  readonly route: Route;
  readonly fleet: FleetPlace;
  readonly go: (action: NavigationAction) => void;
  readonly issues: Issues;
  readonly setIssues: (update: (all: Issues) => Issues) => void;
}): JSX.Element {
  const {hosts, dispatch, route, fleet, go, issues, setIssues} = props;
  const {openHost, openAgent, openRun, costHost, costRun} = openOf(route);
  // The open agent's stream, and the open run's history, which the run stream does not carry: both are
  // followed while a screen of them is open.
  useAtomMount(
    useMemo(
      () => watchOpen(hosts, openHost, openAgent, openRun),
      [hosts, openHost, openAgent, openRun],
    ),
  );
  // The cost stream is open only while the Cost view shows: leaving it closes the stream.
  useAtomMount(useMemo(() => watchCost(hosts, costHost, costRun), [hosts, costHost, costRun]));
  const clock = useAtomValue(nowMs);
  const view = useAtomValue(
    useMemo(
      () =>
        snapshots(
          hosts,
          issues,
          openHost,
          openAgent,
          openRun,
          costHost === undefined || costRun === undefined ? null : {host: costHost, runId: costRun},
        ),
      [hosts, issues, openHost, openAgent, openRun, costHost, costRun],
    ),
  );
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
    retryStart: () => retry(issues.start),
    stopRun: (host, runId) =>
      issue(host, stopIntent(runId), stop =>
        setIssues(all => ({...all, stops: {...all.stops, [runId]: stop}})),
      ),
    retryStop: runId => retry(issues.stops[runId]),
    sendMessage: (host, agentId, text) =>
      issue(host, sendIntent(agentId, text), send =>
        setIssues(all => ({...all, sends: {...all.sends, [agentId]: send}})),
      ),
    retrySend: agentId => retry(issues.sends[agentId]),
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
        const open = openHost === host;
        retryFeeds(dispatch, session, open ? openAgent : undefined, open ? openRun : undefined);
      }}
    />
  );
}
