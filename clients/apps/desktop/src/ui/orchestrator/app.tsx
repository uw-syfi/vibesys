import type {Agent} from '@vibesys/orchestrator-proto';
import {Page, pageState, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {HistoryStatus} from '../../core/index.js';
import {
  type CommandProgress,
  type FeedStatus,
  type HostConnectionState,
  type HostFreshness,
  type HostModel,
  type Route,
  type RouteAction,
  type RunView,
  staleForMs,
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
  /** Whether what is shown of this host is current, and when it last was. */
  readonly freshness: HostFreshness;
  /** The run stream's status. */
  readonly runsFeed: FeedStatus;
  /** The status of each agent's stream the screen has open, by agent id. */
  readonly agentFeeds: Readonly<Record<string, FeedStatus>>;
  /** Where the reads of the open agent's past stand: earlier turns, and each large body by ordinal. */
  readonly history: {
    readonly earlier: HistoryStatus;
    readonly wholes: Readonly<Record<number, HistoryStatus>>;
  };
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
  /** Read the page of items of an agent before ordinal `before`. */
  readonly loadEarlier: (host: string, agentId: string, before: number) => void;
  /** Read the item at `ordinal` of an agent whole. */
  readonly expand: (host: string, agentId: string, ordinal: number) => void;
}

const hasData = (host: HostSnapshot): boolean => host.model.data.sequence > 0n;

function runsNote(
  host: HostSnapshot,
  retry: (host: string) => void,
  nowMs: number,
): ConnectionNote {
  return {
    key: `runs/${host.key}`,
    what: `the runs on ${host.label}`,
    connection: host.connection,
    feed: host.runsFeed,
    hasData: hasData(host),
    staleForMs: staleForMs(host.freshness, nowMs),
    tryAgain: () => retry(host.key),
  };
}

/** The panel of the agent that is open, bound to the commands of its host. */
function OpenAgent(props: {
  readonly state: HostSnapshot['model']['data'];
  readonly agent: Agent;
  readonly host: HostSnapshot;
  readonly nowMs: number;
  readonly commands: AppCommands;
  readonly go: (action: RouteAction) => void;
}): JSX.Element {
  const {agent, host, commands, go} = props;
  return (
    <AgentPanel
      nowMs={props.nowMs}
      state={props.state}
      agent={agent}
      host={host.label}
      history={{
        ...host.history,
        loadEarlier: before => commands.loadEarlier(host.key, agent.id, before),
        expand: ordinal => commands.expand(host.key, agent.id, ordinal),
      }}
      open={agentId => go({type: 'pick-agent', agentId})}
      sendProgress={commands.send[agent.id]}
      send={text => commands.sendMessage(host.key, agent.id, text)}
      retrySend={() => commands.retrySend(agent.id)}
      close={() => go({type: 'close-agent'})}
    />
  );
}

/** The note about one agent's steps on `host`, which share the host's connection. */
function agentNote(
  host: HostSnapshot,
  agent: Agent,
  names: ReturnType<typeof agentNames>,
  props: {readonly nowMs: number; readonly retryHost: (host: string) => void},
): ConnectionNote {
  return {
    key: `agent/${agent.id}`,
    what: `the steps of ${nameOf(names, agent)}`,
    connection: host.connection,
    feed: host.agentFeeds[agent.id] ?? {_tag: 'Opening'},
    hasData: Object.values(host.model.data.items).some(item => item.agentId === agent.id),
    staleForMs: staleForMs(host.freshness, props.nowMs),
    tryAgain: () => props.retryHost(host.key),
  };
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
          <ConnectionBanner
            notes={hosts.map(host => runsNote(host, props.retryHost, props.nowMs))}
          />
          <StartRunForm
            hosts={hosts.map(({key, label}) => ({key, label}))}
            progress={commands.start}
            start={commands.startRun}
            retry={commands.retryStart}
            openRun={(host, runId) => go({type: 'open-run', host, runId})}
          />
          <RunList
            unreadHosts={hosts
              .filter(host => host.runsFeed._tag !== 'Live')
              .map(host => host.label)}
            hosts={hosts.map(host => ({key: host.key, label: host.label, state: host.model.data}))}
            open={(host, runId) => go({type: 'open-run', host, runId})}
          />
        </Stack>
      </Page>
    );
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
  const title = `${state.runs[runId]?.task ?? 'Run'}: ${agent === undefined ? RUN_TITLES[route.view] : nameOf(names, agent)}`;
  const notes = [
    runsNote(host, props.retryHost, props.nowMs),
    ...(agent === undefined ? [] : [agentNote(host, agent, names, props)]),
  ];
  return (
    <Page key="run" width="fill" state="ready" header={<Text variant="title">{title}</Text>}>
      <Stack gap="space-4">
        <ConnectionBanner notes={notes} />
        <RunScreen
          state={state}
          host={host.label}
          runId={runId}
          nowMs={props.nowMs}
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
              <OpenAgent
                state={state}
                agent={agent}
                host={host}
                nowMs={props.nowMs}
                commands={commands}
                go={go}
              />
            )
          }
        />
      </Stack>
    </Page>
  );
}
