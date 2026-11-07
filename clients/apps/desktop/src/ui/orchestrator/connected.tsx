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
import type {HistoryStatus, HostSession, Intent} from '../../core/index.js';
import type {
  CommandProgress,
  CostFeedSnapshot,
  CostTarget,
  FeedStatus,
  Route,
  RouteAction,
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

/** One agent's stream on its host, followed while this is mounted. */
const watchAgent = (
  hosts: readonly ConnectedHost[],
  host: string | undefined,
  agentId: string | null | undefined,
): Atom.Atom<void> =>
  Atom.make(get => {
    const found = hosts.find(candidate => candidate.key === host);
    if (found !== undefined && agentId !== null && agentId !== undefined)
      get(found.session.agents.watch(agentId));
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

/** Everything the screens show, read from the sessions. */
const snapshots = (
  hosts: readonly ConnectedHost[],
  issues: Issues,
  openHost: string | undefined,
  openAgent: string | null | undefined,
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
        history:
          openHost === key && openAgent !== null && openAgent !== undefined
            ? {
                earlier: get(session.history.earlier(openAgent)),
                wholes: get(session.history.wholes(openAgent)),
              }
            : {earlier: IDLE, wholes: {}},
      })),
      progress: {
        start: progressOf(issues.start),
        stop: each(issues.stops),
        send: each(issues.sends),
      },
    };
  });

type AppCommandsProgress = Pick<AppCommands, 'start' | 'stop' | 'send'>;

/**
 * The runs of every host, one open at a time. The run streams are followed by the window (the
 * shell mounts them), and where the person is comes from the shell's navigation.
 */
export function ConnectedApp(props: {
  readonly hosts: readonly ConnectedHost[];
  readonly dispatch: Dispatch;
  readonly route: Route;
  readonly go: (action: RouteAction) => void;
  readonly issues: Issues;
  readonly setIssues: (update: (all: Issues) => Issues) => void;
}): JSX.Element {
  const {hosts, dispatch, route, go, issues, setIssues} = props;
  const openHost = route.screen === 'run' ? route.host : undefined;
  const openAgent = route.screen === 'run' ? route.agentId : undefined;
  useAtomMount(useMemo(() => watchAgent(hosts, openHost, openAgent), [hosts, openHost, openAgent]));
  // The cost stream is open only while the Cost view shows: leaving it closes the stream.
  const cost = costTarget(route);
  const costHost = cost?.host;
  const costRun = cost?.runId;
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
          costHost === undefined || costRun === undefined ? null : {host: costHost, runId: costRun},
        ),
      [hosts, issues, openHost, openAgent, costHost, costRun],
    ),
  );
  const sessionOf = (host: string): HostSession | undefined =>
    hosts.find(candidate => candidate.key === host)?.session;
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
    loadEarlier: (host, agentId, before) => {
      const session = sessionOf(host);
      if (session !== undefined)
        dispatch(session.history.loadEarlier(agentId, before), () => undefined);
    },
    expand: (host, agentId, ordinal) => {
      const session = sessionOf(host);
      if (session !== undefined)
        dispatch(session.history.expand(agentId, ordinal), () => undefined);
    },
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
      go={go}
      retryCost={(host, runId) => {
        const session = sessionOf(host);
        if (session !== undefined) dispatch(session.costs.retry(runId), () => undefined);
      }}
      retryHost={host => {
        const session = sessionOf(host);
        if (session === undefined) return;
        dispatch(session.connection.retry, () => undefined);
        dispatch(session.runs.retry, () => undefined);
        if (openHost === host && openAgent !== null && openAgent !== undefined)
          dispatch(session.agents.retry(openAgent), () => undefined);
      }}
    />
  );
}
