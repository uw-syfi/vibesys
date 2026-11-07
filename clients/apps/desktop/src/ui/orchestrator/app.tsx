import {Page, pageState, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type CommandProgress,
  type FeedStatus,
  freshnessOf,
  type HostConnectionState,
  type HostModel,
  type Route,
  type RouteAction,
  type RunView,
} from '../../state/index.js';
import {AgentPanel} from './agent-panel.js';
import {ConnectionBanner, type ConnectionNote} from './connection-banner.js';
import {agentNames, nameOf} from './names.js';
import {RunList} from './run-list.js';
import {RunScreen} from './run-screen.js';
import type {StartRunRequest} from './start-draft.js';
import {StartRunForm} from './start-run-form.js';

const TITLE = 'Runs';
const RUN_TITLES = {
  report: 'Report',
  timeline: 'Search and activity',
  agents: 'Agents',
  cost: 'Cost',
  handoffs: 'Handoffs and reports',
  ideas: 'Ideas',
} satisfies Record<RunView, string>;

/** Everything the screens know about one host at this moment. */
export interface HostSnapshot {
  /** The `HostKey` of the host: what a route names. */
  readonly key: string;
  /** What the host is called on screen. */
  readonly label: string;
  readonly model: HostModel;
  readonly connection: HostConnectionState;
  /** The run stream's status. */
  readonly runsFeed: FeedStatus;
  /** The status of each agent's stream the screen has open, by agent id. */
  readonly agentFeeds: Readonly<Record<string, FeedStatus>>;
  /** The status of each run's cost stream a screen has open, by run id. */
  readonly costFeeds: Readonly<Record<string, FeedStatus>>;
}

/** What the person has asked for, how it is going, and the ways to ask for more. */
export interface AppCommands {
  /** The last press of Start, or undefined before any. */
  readonly start: CommandProgress | undefined;
  /** The last Stop of each run, by run id. */
  readonly stop: Readonly<Record<string, CommandProgress>>;
  /** The last Send to each agent, by agent id. */
  readonly send: Readonly<Record<string, CommandProgress>>;
  readonly startRun: (host: string, request: StartRunRequest) => void;
  readonly retryStart: () => void;
  readonly stopRun: (host: string, runId: string) => void;
  readonly retryStop: (runId: string) => void;
  readonly sendMessage: (host: string, agentId: string, text: string) => void;
  readonly retrySend: (agentId: string) => void;
}

const hasData = (host: HostSnapshot): boolean => host.model.data.sequence > 0n;

function runsNote(host: HostSnapshot, retry: (host: string) => void): ConnectionNote {
  return {
    key: `runs/${host.key}`,
    what: `the runs on ${host.label}`,
    connection: host.connection,
    feed: host.runsFeed,
    hasData: hasData(host),
    tryAgain: () => retry(host.key),
  };
}

/** The title of a run's page: its name alone on the report, else the name and what is open. */
function runTitle(
  state: HostSnapshot['model']['data'],
  route: Extract<Route, {screen: 'run'}>,
  names: ReturnType<typeof agentNames>,
): string {
  const task = state.runs[route.runId]?.task ?? 'Run';
  const agent = route.agentId === null ? undefined : state.agents[route.agentId];
  if (agent !== undefined) return `${task}: ${nameOf(names, agent)}`;
  return route.view === 'report' ? task : `${task}: ${RUN_TITLES[route.view]}`;
}

/** The list of every connected host's runs, and the form that starts one. */
function RunsPage(props: {
  readonly hosts: readonly HostSnapshot[];
  readonly commands: AppCommands;
  readonly retryHost: (host: string) => void;
  readonly go: (action: RouteAction) => void;
}): JSX.Element {
  const {hosts, commands, go} = props;
  return (
    <Page
      key="runs"
      width="wide"
      state={pageState(hosts.length === 0)}
      header={
        <Text variant="title">
          {hosts.some(host => host.connection._tag === 'Unusable')
            ? 'Runs unavailable'
            : commands.start?._tag === 'Refused'
              ? 'Start run refused'
              : TITLE}
        </Text>
      }
    >
      <Stack gap="space-4">
        <ConnectionBanner notes={hosts.map(host => runsNote(host, props.retryHost))} />
        <StartRunForm
          hosts={hosts.map(({key, label}) => ({key, label}))}
          progress={commands.start}
          start={commands.startRun}
          retry={commands.retryStart}
          openRun={(host, runId) => go({type: 'open-run', host, runId})}
        />
        <RunList
          unreadHosts={hosts.filter(host => host.runsFeed._tag !== 'Live').map(host => host.label)}
          hosts={hosts.map(host => ({key: host.key, label: host.label, state: host.model.data}))}
          open={(host, runId) => go({type: 'open-run', host, runId})}
        />
      </Stack>
    </Page>
  );
}

/** The orchestrator window: every connected host's runs, and one run open at a time. */
export function OrchestratorApp(props: {
  readonly hosts: readonly HostSnapshot[];
  /** Milliseconds since the epoch; the caller owns the clock. */
  readonly nowMs: number;
  readonly commands: AppCommands;
  /** The person pressed Retry on a host's connection or feed. */
  readonly retryHost: (host: string) => void;
  /** Where the person is; the caller owns it so it can watch what that screen needs. */
  readonly route: Route;
  readonly go: (action: RouteAction) => void;
}): JSX.Element {
  const {hosts, commands, route, go} = props;
  if (route.screen === 'runs') {
    return <RunsPage hosts={hosts} commands={commands} retryHost={props.retryHost} go={go} />;
  }
  const host = hosts.find(candidate => candidate.key === route.host);
  if (host === undefined) {
    return (
      <Page key="run" state="loading" header={<Text variant="title">{TITLE}</Text>}>
        <Text tone="muted">Connecting.</Text>
      </Page>
    );
  }
  const state = host.model.data;
  const {runId} = route;
  const agent = route.agentId === null ? undefined : state.agents[route.agentId];
  const names = agentNames(state, runId);
  const notes = [
    runsNote(host, props.retryHost),
    ...(agent === undefined
      ? []
      : [
          {
            key: `agent/${agent.id}`,
            what: `the steps of ${nameOf(names, agent)}`,
            connection: host.connection,
            feed: host.agentFeeds[agent.id] ?? ({_tag: 'Opening'} as const),
            hasData: Object.values(state.items).some(item => item.agentId === agent.id),
            tryAgain: () => props.retryHost(host.key),
          } satisfies ConnectionNote,
        ]),
  ];
  return (
    <Page
      key="run"
      width="fill"
      state="ready"
      header={<Text variant="title">{runTitle(state, route, names)}</Text>}
    >
      <Stack gap="space-4">
        <ConnectionBanner notes={notes} />
        <RunScreen
          state={state}
          host={host.label}
          runId={runId}
          nowMs={props.nowMs}
          freshness={freshnessOf(host.connection, host.runsFeed, hasData(host))}
          costFeed={host.costFeeds[runId] ?? ({_tag: 'Opening'} as const)}
          view={route.view}
          agentId={route.agentId}
          stopProgress={commands.stop[runId]}
          stop={() => commands.stopRun(host.key, runId)}
          retryStop={() => commands.retryStop(runId)}
          goRuns={() => go({type: 'close-run'})}
          show={view => go({type: 'show', view})}
          pick={agentId => go({type: 'pick-agent', agentId})}
          agentPanel={
            agent === undefined ? null : (
              <AgentPanel
                nowMs={props.nowMs}
                state={state}
                agent={agent}
                name={nameOf(names, agent)}
                sendProgress={commands.send[agent.id]}
                send={text => commands.sendMessage(host.key, agent.id, text)}
                retrySend={() => commands.retrySend(agent.id)}
                close={() => go({type: 'close-agent'})}
              />
            )
          }
        />
      </Stack>
    </Page>
  );
}
