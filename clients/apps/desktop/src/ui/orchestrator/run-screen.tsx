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
  type FeedStatus,
  type Freshness,
  flattenTree,
  type OrchestratorState,
  type RunView,
  runReport,
} from '../../state/index.js';
import {CommandLine} from './command-line.js';
import {CostView} from './cost-view.js';
import {HandoffCards} from './handoff-cards.js';
import {agentNames} from './names.js';
import {ReportScreen} from './report/report-screen.js';
import {SearchPanels} from './search-panels.js';
import {TimelineView} from './timeline-view.js';
import {agentWord, harnessWord, runWord} from './words.js';

const VIEWS = [
  {value: 'timeline', label: 'Timeline'},
  {value: 'cost', label: 'Cost'},
  {value: 'handoffs', label: 'Handoffs and reports'},
] as const;

/** The last step of the trail on a screen of the run that is not its report. */
const VIEW_CRUMBS = {
  report: 'Agents',
  timeline: 'Agents',
  agents: 'Agents',
  cost: 'Cost',
  handoffs: 'Handoffs',
  ideas: 'Ideas',
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

/** One run: its agents beside either the chosen agent's turns or the run-wide views. */
export function RunScreen(props: {
  readonly state: OrchestratorState;
  /** What the host is called on screen. */
  readonly host: string;
  readonly runId: string;
  readonly nowMs: number;
  /** How current the run stream is. */
  readonly freshness: Freshness;
  /** The state of the cost stream, which is followed while the report shows. */
  readonly costFeed: FeedStatus;
  readonly view: RunView;
  readonly agentId: string | null;
  readonly agentPanel: ReactNode;
  /** What the last press of Stop came to, or undefined before any. */
  readonly stopProgress: CommandProgress | undefined;
  readonly stop: () => void;
  readonly retryStop: () => void;
  readonly goRuns: () => void;
  readonly show: (view: RunView) => void;
  readonly pick: (agentId: string) => void;
}): JSX.Element {
  const run = props.state.runs[props.runId];
  if (run === undefined) {
    return <Text tone="muted">This run is not on this host any more.</Text>;
  }
  const report = runReport(props.state, props.runId, props.nowMs);
  if (props.view === 'report' && props.agentId === null && report !== undefined) {
    return (
      <ReportScreen
        report={report}
        host={props.host}
        nowMs={props.nowMs}
        freshness={props.freshness}
        costFeed={props.costFeed}
        stopProgress={props.stopProgress}
        stop={props.stop}
        retryStop={props.retryStop}
        goRuns={props.goRuns}
        openAgents={() => props.show('agents')}
        openCost={() => props.show('cost')}
      />
    );
  }
  const search = props.state.searches[props.runId];
  const status = runWord(run.status);
  const stoppable = run.status === RunStatus.STARTING || run.status === RunStatus.RUNNING;
  const body =
    props.view === 'timeline' || props.view === 'agents' || props.view === 'report' ? (
      <TimelineView
        state={props.state}
        runId={props.runId}
        nowMs={props.nowMs}
        agentId={props.agentId}
        pick={props.pick}
      />
    ) : props.view === 'cost' ? (
      <CostView state={props.state} runId={props.runId} />
    ) : (
      <HandoffCards state={props.state} runId={props.runId} />
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
        asideSticky
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
              value={props.view === 'agents' || props.view === 'report' ? 'timeline' : props.view}
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
