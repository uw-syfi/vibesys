import type {Agent} from '@vibesys/orchestrator-proto';
import {Page, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {HistoryStatus} from '../../core/index.js';
import {
  type CommandProgress,
  type CostFeedSnapshot,
  type FeedStatus,
  type FleetHostInput,
  type FleetPlace,
  freshnessOf,
  type HostConnectionState,
  type HostFreshness,
  type HostModel,
  type NavigationAction,
  type Route,
  type RunView,
  staleForMs,
} from '../../state/index.js';
import {AgentPanel} from './agent-panel.js';
import {ConnectionBanner, type ConnectionNote} from './connection-banner.js';
import {FleetScreen} from './fleet-screen.js';
import {agentNames, nameOf} from './names.js';
import {NewRun} from './new-run.js';
import {RunScreen} from './run-screen.js';
import type {StartRunRequest} from './start-draft.js';

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
  /** How each run's cost stream stands while its Cost view is showing, by run id. */
  readonly costFeeds: Readonly<Record<string, CostFeedSnapshot>>;
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

/** A cost stream that has not answered yet. */
const OPENING_COST: CostFeedSnapshot = {status: {_tag: 'Opening'}, freshness: 'empty'};

const hasData = (host: HostSnapshot): boolean => host.model.data.sequence > 0n;

/** A host as the fleet reads it: its runs, how far to trust them, and how long they have been out of date. */
const fleetInput = (host: HostSnapshot, nowMs: number): FleetHostInput => ({
  key: host.key,
  label: host.label,
  data: host.model.data,
  freshness: freshnessOf(host.connection, host.runsFeed, hasData(host)),
  staleForMs: staleForMs(host.freshness, nowMs),
});

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
  readonly go: (action: NavigationAction) => void;
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
  /** The person pressed Retry on a run's cost stream. */
  readonly retryCost: (host: string, runId: string) => void;
  /** Where the person is; the caller owns it so it can watch what that screen needs. */
  readonly route: Route;
  /** The run list's filters and selected row, which the caller keeps while a run is open. */
  readonly fleet: FleetPlace;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element {
  const {hosts, commands, route, go} = props;
  if (route.screen === 'runs') {
    return (
      <FleetScreen
        hosts={hosts.map(host => fleetInput(host, props.nowMs))}
        place={props.fleet}
        nowMs={props.nowMs}
        go={go}
        retryHost={props.retryHost}
        newRun={
          <NewRun
            hosts={hosts.map(({key, label}) => ({key, label}))}
            progress={commands.start}
            start={commands.startRun}
            retry={commands.retryStart}
            openRun={(host, runId) => go({type: 'open-run', host, runId})}
          />
        }
      />
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
          costFeed={host.costFeeds[runId] ?? OPENING_COST}
          retryCost={() => props.retryCost(host.key, runId)}
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
