import {
  type Agent,
  type ListCatalogueResponse,
  type ListEvalPlacesResponse,
  type Run,
  RunStatus,
} from '@vibesys/orchestrator-proto';
import {Button, Chip, type Crumb, Page, Row, Stack, Text, withoutPaths} from '@vibesys/ui';
import type {JSX} from 'react';
import type {HistoryStatus, ProvisionExample, RunHistoryStatus} from '../../core/index.js';
import {
  type CommandProgress,
  type CostFeedSnapshot,
  type FeedStatus,
  type FleetHostInput,
  type FleetPlace,
  type FolderListing,
  followedAgent,
  freshnessOf,
  type GenericRunEntry,
  type GenericRunIndex,
  type HostConnectionState,
  type HostFreshness,
  type HostModel,
  type HostStatus,
  holdsSteps,
  hostStatusOf,
  type IdeaWindow,
  inSetup,
  type NavigationAction,
  type OrchestratorState,
  type ProvisionProgress,
  type Remote,
  type RepositoryChecked,
  type Route,
  runReport,
  runSettingsOf,
  settingsTimeline,
  setupLead,
  setupOf,
  staleForMs,
  UNREAD_SETUP,
} from '../../state/index.js';
import {AgentPage} from '../agents/agent-page.js';
import {AgentsScreen} from '../agents/agents-screen.js';
import {CostScreen} from '../cost/index.js';
import {useDisplayClock} from '../display-clock.js';
import {type ConnectionNote, ConnectionStatus} from './connection-status.js';
import {costWords} from './cost-words.js';
import {FleetScreen} from './fleet-screen.js';
import {runTitle} from './fleet-words.js';
import {genericIndexUnavailableWord, genericRunTitle, genericRunWord} from './generic-run-words.js';
import {HistoryStatusWord} from './history-status.js';
import {agentNames, nameOf} from './names.js';
import {NewRun} from './new-run.js';
import {ReportScreen} from './report/report-screen.js';
import {isStoppable} from './report/report-title.js';
import {PendingLead, RunSettingsControl} from './run-settings/index.js';
import type {RunUpdateRequest} from './run-settings/update-draft.js';
import type {CompleteSetupRequest} from './setup/index.js';
import {SetupScreen, setupTitle} from './setup/index.js';
import type {StartRunRequest} from './start-draft.js';
import type {StartHost} from './start-run-form.js';

const TITLE = 'Runs';

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
  /** The renderer-safe generic run index at its last atomic publication boundary. */
  readonly runIndex: GenericRunIndex;
  /** The host's latest answer about the repository path typed in the Start form. */
  readonly repositoryChecked: RepositoryChecked | undefined;
  /** The host's listing of each folder the Start form has asked for, by `listingKey`. */
  readonly directories: ReadonlyMap<string, FolderListing>;
  /** The host's catalogue of example tasks, as of its last read. */
  readonly catalogue: Remote<ListCatalogueResponse>;
  /** The host's eval places, as of their last read. */
  readonly evalPlaces: Remote<ListEvalPlacesResponse>;
  /** The Repositories folder kept for this machine (empty when none): where Copy to starts. */
  readonly repositoriesDirectory: string;
  /** Where the repository being made from an example stands. */
  readonly provision: ProvisionProgress;
  /** The status of each agent's stream the screen has open, by agent id. */
  readonly agentFeeds: Readonly<Record<string, FeedStatus>>;
  /** How each run's cost stream stands while its Cost view is showing, by run id. */
  readonly costFeeds: Readonly<Record<string, CostFeedSnapshot>>;
  /** Where the read of the open run's history (agents, turns, messages, check-ins) stands. */
  readonly runHistory: RunHistoryStatus;
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
  /** The last Stop of each agent, by agent id. */
  readonly agentStop: Readonly<Record<string, CommandProgress>>;
  /** The last Send to each agent, by agent id. */
  readonly send: Readonly<Record<string, CommandProgress>>;
  /** The last settings update of each run, by run id. */
  readonly update: Readonly<Record<string, CommandProgress>>;
  /** The last press of Start on each run in setup, by run id. */
  readonly complete: Readonly<Record<string, CommandProgress>>;
  readonly startRun: (host: string, request: StartRunRequest) => void;
  /** The repository path typed in the Start form is now `repository` on `host`. */
  readonly checkRepository: (host: string, repository: string) => void;
  /**
   * Ask `host` for the entries of `folder` (once), hidden ones too when `hidden`. With `fresh` what
   * the host listed before is dropped first, so the folder is read again: the field was entered.
   */
  readonly listDirectory: (host: string, folder: string, hidden: boolean, fresh: boolean) => void;
  /** The New run screen is open on `host`: read its catalogue and eval places. */
  readonly loadNewRun: (host: string) => void;
  /** Make the repository of an example on `host`, then start in it; `request` names no repository yet. */
  readonly startFromExample: (
    host: string,
    example: ProvisionExample,
    request: StartRunRequest,
  ) => void;
  /** Make an empty repository on `host`, then begin a setup chat in it; `request` names no repository yet. */
  readonly setUpInNewRepository: (
    host: string,
    repository: {readonly name: string; readonly parent: string},
    request: StartRunRequest,
  ) => void;
  /** Forget the last press of Start, so a form opened again shows none of it. */
  readonly clearStart: () => void;
  /** Forget what `host` said of the last copy of an example. */
  readonly forgetProvision: (host: string) => void;
  readonly retryStart: () => void;
  readonly stopRun: (host: string, runId: string) => void;
  readonly retryStop: (runId: string) => void;
  /** Stop one agent of a run (not its lead): the host interrupts its turn and closes its session. */
  readonly stopAgent: (host: string, agentId: string) => void;
  readonly retryStopAgent: (agentId: string) => void;
  readonly sendMessage: (host: string, agentId: string, text: string) => void;
  readonly retrySend: (agentId: string) => void;
  /** Change settings of a live run: all of `request` or none of it. */
  readonly updateRun: (host: string, runId: string, request: RunUpdateRequest) => void;
  readonly retryUpdate: (runId: string) => void;
  /** Begin a run in its setup phase: a chat with a lead that makes the task. The answer is `start`'s. */
  readonly beginSetup: (host: string, request: StartRunRequest) => void;
  /** Start optimizing the task of a run in setup; the host decides whether setup is complete. */
  readonly completeSetup: (host: string, runId: string, request: CompleteSetupRequest) => void;
  readonly retryComplete: (runId: string) => void;
  /** Read the page of items of an agent before ordinal `before`. */
  readonly loadEarlier: (host: string, agentId: string, before: number) => void;
  /** Read the item at `ordinal` of an agent whole. */
  readonly expand: (host: string, agentId: string, ordinal: number) => void;
}

/** A cost stream that has not answered yet. */
const OPENING_COST: CostFeedSnapshot = {status: {_tag: 'Opening'}, freshness: 'empty'};

const hasData = (host: HostSnapshot): boolean => host.model.data.sequence > 0n;

/** The one status of a host, as its runs feed reads it. */
const runsStatus = (host: HostSnapshot, nowMs: number): HostStatus =>
  hostStatusOf({
    connection: host.connection,
    feed: host.runsFeed,
    hasData: hasData(host),
    staleForMs: staleForMs(host.freshness, nowMs),
  });

/** A host as the fleet reads it: its runs, how far to trust them, and how long they have been out of date. */
const fleetInput = (host: HostSnapshot, nowMs: number): FleetHostInput => ({
  key: host.key,
  label: host.label,
  data: host.model.data,
  runIndex: host.runIndex,
  freshness: freshnessOf(host.connection, host.runsFeed, hasData(host)),
  status: runsStatus(host, nowMs),
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

/** The page of the agent that is open, bound to the commands of its host. */
function OpenAgent(props: {
  readonly state: OrchestratorState;
  readonly agent: Agent;
  readonly host: HostSnapshot;
  readonly nowMs: number;
  readonly commands: AppCommands;
  readonly trail: readonly Crumb[];
  /** Where the read of the run's history stands, beside the title. */
  readonly reading: JSX.Element | null;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element {
  const {agent, host, commands, go} = props;
  return (
    <AgentPage
      nowMs={props.nowMs}
      state={props.state}
      agent={agent}
      host={host.label}
      trail={props.trail}
      reading={props.reading}
      history={{
        ...host.history,
        loadEarlier: before => commands.loadEarlier(host.key, agent.id, before),
        expand: ordinal => commands.expand(host.key, agent.id, ordinal),
      }}
      open={agentId => go({type: 'pick-agent', agentId})}
      sendProgress={commands.send[agent.id]}
      send={text => commands.sendMessage(host.key, agent.id, text)}
      retrySend={() => commands.retrySend(agent.id)}
      stopProgress={commands.agentStop[agent.id]}
      stop={() => commands.stopAgent(host.key, agent.id)}
      retryStop={() => commands.retryStopAgent(agent.id)}
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
    hasData: holdsSteps(host.model.data, agent.id),
    staleForMs: staleForMs(host.freshness, props.nowMs),
    tryAgain: () => props.retryHost(host.key),
  };
}

/** The hosts as the New run page reads them. */
const startHostsOf = (hosts: readonly HostSnapshot[]): readonly StartHost[] =>
  hosts.map(
    ({
      key,
      label,
      model,
      repositoryChecked,
      directories,
      catalogue,
      evalPlaces,
      repositoriesDirectory,
      provision,
    }) => ({
      key,
      label,
      models: model.data.models,
      repositoryChecked,
      directories,
      catalogue,
      evalPlaces,
      repositoriesDirectory,
      provision,
    }),
  );

/** Every way to New run: read every machine's catalogue, places and Repositories folder for its tabs. */
function openNewRun(
  hosts: readonly HostSnapshot[],
  commands: AppCommands,
  go: (action: NavigationAction) => void,
): void {
  // Every way in opens fresh, with no outcome of an earlier start.
  commands.clearStart();
  for (const host of hosts) {
    commands.forgetProvision(host.key);
    commands.loadNewRun(host.key);
    if (host.repositoriesDirectory !== '') {
      const folder = host.repositoriesDirectory.replace(/\/+$/, '');
      commands.listDirectory(host.key, `${folder}/`, false, true);
    }
  }
  go({type: 'open-new-run'});
}

/** The Runs page's way to New run. */
function NewRunButton(props: {
  readonly hosts: readonly HostSnapshot[];
  readonly commands: AppCommands;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element {
  return (
    <Button
      text="New run"
      icon="plus"
      variant="primary"
      press={() => openNewRun(props.hosts, props.commands, props.go)}
    />
  );
}

/** The page that starts a run, bound to the commands of the window. */
function NewRunPage(props: {
  readonly hosts: readonly HostSnapshot[];
  readonly commands: AppCommands;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element {
  const {commands, go} = props;
  return (
    <NewRun
      hosts={startHostsOf(props.hosts)}
      progress={commands.start}
      start={commands.startRun}
      checkRepository={commands.checkRepository}
      listDirectory={commands.listDirectory}
      retry={commands.retryStart}
      loadNewRun={commands.loadNewRun}
      startFromExample={commands.startFromExample}
      forgetProvision={commands.forgetProvision}
      beginSetup={commands.beginSetup}
      setUpInNewRepository={commands.setUpInNewRepository}
      openRun={(host, runId) => go({type: 'open-run', host, runId})}
      openRuns={() => go({type: 'open-runs'})}
      editMachines={() => go({type: 'open-machines'})}
    />
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
  /** The person pressed Retry on a run's cost stream. */
  readonly retryCost: (host: string, runId: string) => void;
  /** Where the person is; the caller owns it so it can watch what that screen needs. */
  readonly route: Route;
  /** The run list's filters and selected row, which the caller keeps while a run is open. */
  readonly fleet: FleetPlace;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element {
  const {hosts, commands, route, go} = props;
  if (route.screen === 'new-run') return <NewRunPage hosts={hosts} commands={commands} go={go} />;
  if (route.screen === 'runs') {
    return (
      <FleetScreen
        hosts={hosts.map(host => fleetInput(host, props.nowMs))}
        place={props.fleet}
        nowMs={props.nowMs}
        go={go}
        retryHost={props.retryHost}
        newRun={<NewRunButton hosts={hosts} commands={commands} go={go} />}
      />
    );
  }
  const host = hosts.find(candidate => candidate.key === route.host);
  if (host === undefined) {
    return (
      <Page key="run" width="wide" state="loading" title={TITLE}>
        <Text tone="muted">Connecting.</Text>
      </Page>
    );
  }
  return (
    <RunDestination
      route={route}
      hosts={hosts}
      host={host}
      nowMs={props.nowMs}
      commands={commands}
      retryHost={props.retryHost}
      retryCost={props.retryCost}
      go={go}
    />
  );
}

/** The agents of a run, or, with one open, that agent's page one step under them. */
function AgentsDestination(props: {
  readonly state: OrchestratorState;
  readonly runId: string;
  readonly host: HostSnapshot;
  readonly nowMs: number;
  readonly agent: Agent | undefined;
  /** The trail down to the run. */
  readonly trail: readonly Crumb[];
  /** Where the read of the run's history stands, beside the title. */
  readonly reading: JSX.Element | null;
  readonly commands: AppCommands;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element {
  const {state, runId, host, agent, go} = props;
  const trail: readonly Crumb[] = [
    ...props.trail,
    {
      id: 'agents',
      label: 'Agents',
      ...(agent === undefined ? {} : {press: () => go({type: 'close-agent'})}),
    },
  ];
  return agent === undefined ? (
    <AgentsScreen
      state={state}
      runId={runId}
      host={host.label}
      nowMs={props.nowMs}
      trail={trail}
      reading={props.reading}
      open={agentId => go({type: 'pick-agent', agentId})}
    />
  ) : (
    <OpenAgent
      state={state}
      agent={agent}
      host={host}
      nowMs={props.nowMs}
      commands={props.commands}
      trail={trail}
      reading={props.reading}
      go={go}
    />
  );
}

/** The state words beside a run's title: how its streams and history stand, and a report that is missing. */
function runReading(props: {
  readonly host: HostSnapshot;
  readonly runId: string;
  /** The agent whose steps the page follows, if any. */
  readonly agent: Agent | undefined;
  readonly nowMs: number;
  readonly retryHost: (host: string) => void;
}): JSX.Element {
  const {host, runId, agent, nowMs} = props;
  const state = host.model.data;
  const names = agentNames(state, runId);
  return (
    <>
      <ConnectionStatus
        notes={[
          runsNote(host, props.retryHost, nowMs),
          ...(agent === undefined
            ? []
            : [agentNote(host, agent, names, {nowMs, retryHost: props.retryHost})]),
        ]}
      />
      <HistoryStatusWord status={host.runHistory} retry={() => props.retryHost(host.key)} />
      {state.summaries[runId]?.reportUnavailable === true ? (
        <Chip label="Report unavailable" tone="warn" dot />
      ) : null}
    </>
  );
}

/**
 * The page of a run that the route names, whichever it is: the report, the agents, one agent, the
 * cost. Every one is a `Page` of the same frame, with the trail from the Runs list down to it, and
 * none carries another screen's pieces beside it.
 */
function RunDestination(props: {
  readonly route: Extract<Route, {screen: 'run'}>;
  /** Every connected host, which the New run page will offer after this run ends. */
  readonly hosts: readonly HostSnapshot[];
  readonly host: HostSnapshot;
  readonly nowMs: number;
  readonly commands: AppCommands;
  readonly retryHost: (host: string) => void;
  readonly retryCost: (host: string, runId: string) => void;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element {
  const {route, host, commands, go, nowMs} = props;
  const clock = useDisplayClock();
  const state = host.model.data;
  const {runId} = route;
  const run = state.runs[runId];
  const goRuns = (): void => go({type: 'close-run'});
  if (run === undefined) return <GenericOrMissingRun host={host} runId={runId} goRuns={goRuns} />;
  const agent = route.agentId === null ? undefined : state.agents[route.agentId];
  const followed = followedAgent(state, runId, route.agentId);
  const reading = runReading({
    host,
    runId,
    agent: followed === null ? undefined : state.agents[followed],
    nowMs,
    retryHost: props.retryHost,
  });
  const toRun: readonly Crumb[] = [
    {id: 'runs', label: TITLE, press: goRuns},
    {
      id: runId,
      label: inSetup(run) ? setupTitle(run.repository) : runTitle(run.taskCard ?? null, run.task),
      press: () => go({type: 'show', view: 'report'}),
    },
  ];
  if (inSetup(run) && route.view === 'report') {
    return (
      <SetupDestination
        state={state}
        run={run}
        host={host}
        nowMs={nowMs}
        reading={reading}
        commands={commands}
        go={go}
      />
    );
  }
  if (route.view === 'cost') {
    const feed = host.costFeeds[runId] ?? OPENING_COST;
    return (
      <CostScreen
        view={state.costs[runId]}
        status={hostStatusOf({
          connection: host.connection,
          feed: feed.status,
          hasData: state.costs[runId] !== undefined,
          staleForMs: staleForMs(host.freshness, nowMs),
        })}
        feed={feed.status}
        runOver={run.status === RunStatus.ENDED || run.status === RunStatus.FAILED}
        words={costWords(clock)}
        trail={[...toRun, {id: 'cost', label: 'Cost'}]}
        reading={reading}
        retry={() => props.retryCost(host.key, runId)}
      />
    );
  }
  if (route.view === 'agents') {
    return (
      <AgentsDestination
        state={state}
        runId={runId}
        host={host}
        nowMs={nowMs}
        agent={agent}
        trail={toRun}
        reading={reading}
        commands={commands}
        go={go}
      />
    );
  }
  return (
    <ReportDestination
      ideasWindow={route.ideas}
      host={host}
      runId={runId}
      nowMs={nowMs}
      reading={reading}
      commands={commands}
      retryCost={props.retryCost}
      runAgain={() => openNewRun(props.hosts, commands, go)}
      go={go}
    />
  );
}

/** Route a generic-only run without mistaking an absent or unpublished index entry for one. */
function GenericOrMissingRun(props: {
  readonly host: HostSnapshot;
  readonly runId: string;
  readonly goRuns: () => void;
}): JSX.Element {
  const runIndex = props.host.runIndex;
  if (runIndex._tag === 'Dormant' || runIndex._tag === 'Loading')
    return (
      <GenericRunIndexState
        runId={props.runId}
        title="Run index opening"
        detail="Waiting for the host's first complete run-index snapshot."
        goRuns={props.goRuns}
      />
    );
  if (runIndex._tag === 'Unavailable')
    return (
      <GenericRunIndexState
        runId={props.runId}
        title="Run index unavailable"
        detail={genericIndexUnavailableWord(runIndex.reason)}
        goRuns={props.goRuns}
      />
    );
  const run = runIndex.runs.get(props.runId);
  if (run === undefined && runIndex.freshness === 'stale')
    return (
      <GenericRunIndexState
        runId={props.runId}
        title="Run status unconfirmed"
        detail="The last complete run index does not include this run, but that snapshot is stale. Waiting for a current snapshot to confirm."
        goRuns={props.goRuns}
      />
    );
  return run === undefined ? (
    <MissingRun goRuns={props.goRuns} />
  ) : (
    <GenericRunDestination run={run} freshness={runIndex.freshness} goRuns={props.goRuns} />
  );
}

/** A persisted generic route while its independent discovery stream has no whole snapshot. */
function GenericRunIndexState(props: {
  readonly runId: string;
  readonly title: string;
  readonly detail: string;
  readonly goRuns: () => void;
}): JSX.Element {
  return (
    <Page
      key="generic-run-index"
      width="wide"
      state="ready"
      trail={[{id: 'runs', label: TITLE, press: props.goRuns}]}
      title="Run"
    >
      <Stack gap="space-3">
        <Stack gap="space-1">
          <Text as="h2" variant="heading">
            {props.title}
          </Text>
          <Text tone="muted">{props.detail}</Text>
        </Stack>
        <Stack gap="space-1">
          <Text variant="code" title={props.runId}>
            {props.runId}
          </Text>
        </Stack>
      </Stack>
    </Page>
  );
}

/** A generic-only run remains routable while its typed detail feed is introduced separately. */
function GenericRunDestination(props: {
  readonly run: GenericRunEntry;
  readonly freshness: 'current' | 'stale';
  readonly goRuns: () => void;
}): JSX.Element {
  const {run} = props;
  const title = genericRunTitle({projectRoot: run.projectRoot});
  const state = genericRunWord({state: run.state});
  const availability = props.freshness === 'current' ? 'Up to date' : 'Last known';
  return (
    <Page
      key="generic-run"
      width="wide"
      state="ready"
      trail={[
        {id: 'runs', label: TITLE, press: props.goRuns},
        {id: run.runId, label: title},
      ]}
      title={title}
    >
      <Stack gap="space-4">
        <Row gap="space-2" wrap>
          <Chip label={state.label} tone={state.tone} dot />
          <Chip label={availability} tone={props.freshness === 'current' ? 'neutral' : 'warn'} />
          {run.needsUser ? <Chip label="Needs you" tone="warn" dot /> : null}
        </Row>
        {run.needsUser ? (
          <Stack gap="space-1">
            <Text as="h2" variant="heading">
              Input needed
            </Text>
            <Text>
              This run is waiting for a response. Return to the client or workflow that started it
              to continue.
            </Text>
          </Stack>
        ) : null}
        <Stack gap="space-1">
          <Text as="h2" variant="heading">
            Run details unavailable
          </Text>
          <Text tone="muted">
            This run is listed by the host. Detailed activity is not available in this app version.
          </Text>
        </Stack>
        <Stack gap="space-1">
          <Text variant="code" title={run.runId}>
            {run.runId}
          </Text>
          <Text variant="small" tone="muted">
            {run.projectRoot === null
              ? 'Project unavailable'
              : `Project ${withoutPaths(run.projectRoot)}`}
          </Text>
        </Stack>
      </Stack>
    </Page>
  );
}

/** The page of a run in its setup phase, bound to the commands of its host: the chat with its lead beside the draft task. */
function SetupDestination(props: {
  readonly state: OrchestratorState;
  readonly run: Run;
  readonly host: HostSnapshot;
  readonly nowMs: number;
  readonly reading: JSX.Element | null;
  readonly commands: AppCommands;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element {
  const {state, run, host, commands, go} = props;
  const lead = setupLead(state, run.id);
  return (
    <SetupScreen
      state={state}
      run={run}
      facts={setupOf(state, run.id) ?? UNREAD_SETUP}
      lead={lead}
      host={host.label}
      nowMs={props.nowMs}
      trail={[
        {id: 'runs', label: TITLE, press: () => go({type: 'close-run'})},
        {id: run.id, label: setupTitle(run.repository)},
      ]}
      reading={props.reading}
      history={
        lead === undefined
          ? {
              earlier: {_tag: 'Idle'},
              wholes: {},
              loadEarlier: () => undefined,
              expand: () => undefined,
            }
          : {
              ...host.history,
              loadEarlier: before => commands.loadEarlier(host.key, lead.id, before),
              expand: ordinal => commands.expand(host.key, lead.id, ordinal),
            }
      }
      open={agentId => go({type: 'pick-agent', agentId})}
      sendProgress={lead === undefined ? undefined : commands.send[lead.id]}
      send={text => lead !== undefined && commands.sendMessage(host.key, lead.id, text)}
      retrySend={() => lead !== undefined && commands.retrySend(lead.id)}
      completeProgress={commands.complete[run.id]}
      complete={(request: CompleteSetupRequest) =>
        commands.completeSetup(host.key, run.id, request)
      }
      retryComplete={() => commands.retryComplete(run.id)}
      stopProgress={commands.stop[run.id]}
      stop={() => commands.stopRun(host.key, run.id)}
    />
  );
}

/** What a run's page says when the host no longer has the run. */
function MissingRun(props: {readonly goRuns: () => void}): JSX.Element {
  return (
    <Page
      key="run"
      width="wide"
      state="ready"
      trail={[{id: 'runs', label: TITLE, press: props.goRuns}]}
      title="Run"
    >
      <Text tone="muted">This run is not on this host any more.</Text>
    </Page>
  );
}

/** The report of a run, bound to the commands of its host. */
function ReportDestination(props: {
  readonly host: HostSnapshot;
  readonly runId: string;
  /** The check-in span the report's ideas open narrowed to; null for the run's own. */
  readonly ideasWindow: IdeaWindow | null;
  readonly nowMs: number;
  /** Where the read of the run's history stands, beside the title. */
  readonly reading: JSX.Element | null;
  readonly commands: AppCommands;
  readonly retryCost: (host: string, runId: string) => void;
  /** Leave an over run for a fresh New run, prepared exactly as the Runs page prepares it. */
  readonly runAgain: () => void;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element {
  const {host, runId, nowMs, commands, go} = props;
  const state = host.model.data;
  const report = runReport(state, runId, nowMs);
  if (report === undefined) return <MissingRun goRuns={() => go({type: 'close-run'})} />;
  const settings = runSettingsOf(state, runId, nowMs);
  return (
    <ReportScreen
      settings={
        settings === undefined ? null : (
          <RunSettingsControl
            settings={settings}
            timeline={settingsTimeline(state, runId)}
            models={state.models}
            live={isStoppable(report.header.status)}
            progress={commands.update[runId]}
            update={request => commands.updateRun(host.key, runId, request)}
            retry={() => commands.retryUpdate(runId)}
          />
        )
      }
      report={report}
      host={host.label}
      nowMs={nowMs}
      cost={state.costs[runId]}
      costFeed={host.costFeeds[runId] ?? OPENING_COST}
      retryCost={() => props.retryCost(host.key, runId)}
      stopProgress={commands.stop[runId]}
      stop={() => commands.stopRun(host.key, runId)}
      retryStop={() => commands.retryStop(runId)}
      openCatchUp={() => go({type: 'show', view: 'catch-up'})}
      runAgain={props.runAgain}
      goRuns={() => go({type: 'close-run'})}
      openAgents={() => go({type: 'show', view: 'agents'})}
      openCost={() => go({type: 'show', view: 'cost'})}
      names={agentNames(state, runId)}
      reading={
        <>
          {props.reading}
          {settings === undefined ? null : <PendingLead settings={settings} />}
        </>
      }
      ideasWindow={props.ideasWindow}
    />
  );
}
