import {Page, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type CommandProgress,
  type FeedStatus,
  type FleetHostInput,
  type FleetPlace,
  freshnessOf,
  type HostConnectionState,
  type HostModel,
  type NavigationAction,
  type Route,
  type RunView,
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

/** A host as the fleet reads it: its runs, and how far to trust them. */
const fleetInput = (host: HostSnapshot): FleetHostInput => ({
  key: host.key,
  label: host.label,
  data: host.model.data,
  freshness: freshnessOf(host.connection, host.runsFeed, hasData(host)),
  staleForMs: null,
});

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
  /** The run list's filters and selected row, which the caller keeps while a run is open. */
  readonly fleet: FleetPlace;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element {
  const {hosts, commands, route, go} = props;
  if (route.screen === 'runs') {
    return (
      <FleetScreen
        hosts={hosts.map(fleetInput)}
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
