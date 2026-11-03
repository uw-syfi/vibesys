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
import {type JSX, useMemo, useReducer, useState} from 'react';
import type {HostSession, Intent} from '../../core/index.js';
import type {CommandProgress, FeedStatus} from '../../state/index.js';
import {initialRoute, routeReducer} from '../../state/index.js';
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

interface Issues {
  readonly start: Issued | undefined;
  readonly stops: Readonly<Record<string, Issued>>;
  readonly sends: Readonly<Record<string, Issued>>;
}

const NONE: Issues = {start: undefined, stops: {}, sends: {}};

const OPENING: FeedStatus = {_tag: 'Opening'};

/** The run streams of every host, followed while this is mounted. */
const watchRuns = (hosts: readonly ConnectedHost[]): Atom.Atom<void> =>
  Atom.make(get => {
    for (const host of hosts) get(host.session.runs.watch);
  });

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

/** Everything the screens show, read from the sessions. */
const snapshots = (
  hosts: readonly ConnectedHost[],
  issues: Issues,
  openHost: string | undefined,
  openAgent: string | null | undefined,
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
        runsFeed: get(session.runs.status),
        agentFeeds:
          openHost === key && openAgent !== null && openAgent !== undefined
            ? {[openAgent]: get(session.agents.status(openAgent)) ?? OPENING}
            : {},
      })),
      progress: {
        start: progressOf(issues.start),
        stop: each(issues.stops),
        send: each(issues.sends),
      },
    };
  });

type AppCommandsProgress = Pick<AppCommands, 'start' | 'stop' | 'send'>;

export function ConnectedApp(props: {
  readonly hosts: readonly ConnectedHost[];
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {hosts, dispatch} = props;
  const [route, go] = useReducer(routeReducer, initialRoute);
  const [issues, setIssues] = useState<Issues>(NONE);
  const openHost = route.screen === 'run' ? route.host : undefined;
  const openAgent = route.screen === 'run' ? route.agentId : undefined;
  useAtomMount(useMemo(() => watchRuns(hosts), [hosts]));
  useAtomMount(useMemo(() => watchAgent(hosts, openHost, openAgent), [hosts, openHost, openAgent]));
  const clock = useAtomValue(nowMs);
  const view = useAtomValue(
    useMemo(
      () => snapshots(hosts, issues, openHost, openAgent),
      [hosts, issues, openHost, openAgent],
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
