import {RunStatus} from '@vibesys/orchestrator-proto';
import {
  Breadcrumb,
  Button,
  Card,
  Chip,
  Row,
  Split,
  Stack,
  Tabs,
  Text,
  Tree,
  TreeRow,
} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  agentTree,
  type CommandProgress,
  type CostFeedSnapshot,
  type Freshness,
  flattenTree,
  type OrchestratorState,
  type RunView,
  runReport,
} from '../../state/index.js';
import {AgentsScreen} from '../agents/agents-screen.js';
import {CostScreen} from '../cost/index.js';
import {CommandLine} from './command-line.js';
import {COST_WORDS} from './cost-words.js';
import {HandoffCards} from './handoff-cards.js';
import {agentNames} from './names.js';
import {ReportScreen} from './report/report-screen.js';
import {SearchPanels} from './search-panels.js';
import {TimelineView} from './timeline-view.js';
import {agentWord, harnessWord, runWord} from './words.js';

const VIEWS = [
  {value: 'timeline', label: 'Timeline'},
  {value: 'agents', label: 'Agents'},
  {value: 'cost', label: 'Cost'},
  {value: 'handoffs', label: 'Handoffs and reports'},
] as const;

/** The last step of the trail on a screen of the run that is not its report. */
const VIEW_CRUMBS = {
  report: 'Agents',
  timeline: 'Timeline',
  agents: 'Agents',
  cost: 'Cost',
  handoffs: 'Handoffs',
  ideas: 'Ideas',
  'catch-up': 'Catch me up',
} satisfies Record<RunView, string>;

function AgentTree(props: {
  readonly state: OrchestratorState;
  readonly runId: string;
  readonly agentId: string | null;
  readonly pick: (agentId: string) => void;
}): JSX.Element {
  const names = agentNames(props.state, props.runId);
  return (
    <Card section="Agents" flush>
      <Tree
        label="Agents in this run"
        {...(props.agentId === null ? {} : {selected: props.agentId})}
      >
        {flattenTree(agentTree(props.state, props.runId)).map(({agent, depth}) => {
          const word = agentWord(agent.status);
          return (
            <TreeRow
              key={agent.id}
              id={agent.id}
              depth={depth}
              title={names.get(agent.id) ?? 'An agent'}
              chips={[{label: harnessWord(agent.harness), tone: 'neutral'}]}
              state={{tone: word.tone, label: word.label}}
              select={() => props.pick(agent.id)}
            />
          );
        })}
      </Tree>
    </Card>
  );
}

interface RunScreenProps {
  readonly state: OrchestratorState;
  /** What the host is called on screen. */
  readonly host: string;
  readonly runId: string;
  readonly nowMs: number;
  /** How current the run stream is. */
  readonly freshness: Freshness;
  /** How long what is shown has been out of date, from the host's freshness; null while it is current. */
  readonly staleForMs: number | null;
  readonly view: RunView;
  /** How the run's cost stream is going; it is open while the report or the Cost view is showing. */
  readonly costFeed: CostFeedSnapshot;
  readonly retryCost: () => void;
  readonly agentId: string | null;
  readonly agentPanel: ReactNode;
  /** What the last press of Stop came to, or undefined before any. */
  readonly stopProgress: CommandProgress | undefined;
  readonly stop: () => void;
  readonly retryStop: () => void;
  /** Press Catch me up on this run and open its page. */
  readonly catchUp: () => void;
  readonly goRuns: () => void;
  readonly show: (view: RunView) => void;
  readonly pick: (agentId: string) => void;
}

/**
 * One run: it opens on its report. The agents, the cost and the other screens of the run open from
 * the report, each with its agents beside either the chosen agent's turns or the run-wide views.
 */
export function RunScreen(props: RunScreenProps): JSX.Element {
  const report = runReport(props.state, props.runId, props.nowMs);
  if (report === undefined) {
    return <Text tone="muted">This run is not on this host any more.</Text>;
  }
  if (props.view !== 'report' || props.agentId !== null) return <DrillDown {...props} />;
  return (
    <ReportScreen
      report={report}
      host={props.host}
      nowMs={props.nowMs}
      freshness={props.freshness}
      staleForMs={props.staleForMs}
      cost={props.state.costs[props.runId]}
      costFeed={props.costFeed}
      retryCost={props.retryCost}
      stopProgress={props.stopProgress}
      stop={props.stop}
      retryStop={props.retryStop}
      catchUp={props.catchUp}
      goRuns={props.goRuns}
      openAgents={() => props.show('agents')}
      openCost={() => props.show('cost')}
      names={agentNames(props.state, props.runId)}
    />
  );
}

/** What the chosen view of the run shows: the timeline, the agents, the cost or the handoffs. */
function ViewBody(props: RunScreenProps & {readonly over: boolean}): JSX.Element {
  return props.view === 'timeline' || props.view === 'report' ? (
    <TimelineView
      state={props.state}
      runId={props.runId}
      nowMs={props.nowMs}
      agentId={props.agentId}
      pick={props.pick}
    />
  ) : props.view === 'agents' ? (
    <AgentsScreen
      state={props.state}
      runId={props.runId}
      host={props.host}
      nowMs={props.nowMs}
      selected={props.agentId}
      open={props.pick}
    />
  ) : props.view === 'cost' ? (
    <CostScreen
      view={props.state.costs[props.runId]}
      freshness={props.costFeed.freshness}
      status={props.costFeed.status}
      runOver={props.over}
      words={COST_WORDS}
      retry={props.retryCost}
    />
  ) : (
    <HandoffCards state={props.state} runId={props.runId} />
  );
}

/** A screen of the run that is not its report: its agents, its cost, its handoffs. */
function DrillDown(props: RunScreenProps): JSX.Element {
  const run = props.state.runs[props.runId];
  if (run === undefined) return <Text tone="muted">This run is not on this host any more.</Text>;
  const search = props.state.searches[props.runId];
  const status = runWord(run.status);
  const stoppable = run.status === RunStatus.STARTING || run.status === RunStatus.RUNNING;
  const body = (
    <ViewBody {...props} over={run.status === RunStatus.ENDED || run.status === RunStatus.FAILED} />
  );
  return (
    <Stack gap="space-4">
      <Row gap="space-3" wrap>
        <Breadcrumb
          label="Where you are"
          items={[
            {id: 'runs', label: 'Runs', press: props.goRuns},
            {id: props.runId, label: run.task, press: () => props.show('report')},
            {id: props.view, label: VIEW_CRUMBS[props.view]},
          ]}
        />
        <Chip label={status.label} tone={status.tone} dot />
        <Chip label={props.host} mono />
        {stoppable ? (
          <Button
            text="Stop run"
            label="Stop run"
            variant="danger"
            size="sm"
            press={props.stop}
            {...(props.stopProgress?._tag === 'Sending'
              ? {disabledReason: 'Stopping.', quietReason: true}
              : {})}
          />
        ) : null}
      </Row>
      <CommandLine progress={props.stopProgress} done="Stop" retry={props.retryStop} />
      {search === undefined ? null : <SearchPanels search={search} nowMs={props.nowMs} />}
      <Split
        asideFirst
        aside={
          <AgentTree
            state={props.state}
            runId={props.runId}
            agentId={props.agentId}
            pick={props.pick}
          />
        }
        main={
          props.agentPanel ?? (
            <Tabs
              label="Views of this run"
              tabs={VIEWS}
              value={props.view === 'report' ? 'timeline' : props.view}
              change={props.show}
            >
              {body}
            </Tabs>
          )
        }
      />
    </Stack>
  );
}
