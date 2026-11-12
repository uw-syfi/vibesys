import {RunStatus} from '@vibesys/orchestrator-proto';
import {MainSide, Page, Row, Stack} from '@vibesys/ui';
import {type JSX, type ReactNode, useState} from 'react';
import type {
  CommandProgress,
  CostFeedSnapshot,
  CostView,
  IdeaWindow,
  RunReport,
} from '../../../state/index.js';
import {CommandLine} from '../command-line.js';
import {runTitle} from '../fleet-words.js';
import {AgentsAndCost} from './agents-cost.js';
import {FactsLine} from './facts-line.js';
import {IdeasPanel} from './ideas-panel.js';
import {LatestMeasurements} from './latest-measurements.js';
import {LeadSummary} from './lead-summary.js';
import {PerformancePanel} from './performance.js';
import {PlansSummary} from './plans-summary.js';
import {isStoppable, ReportActions, ReportMeta} from './report-title.js';
import {TradeOff} from './trade-off.js';
import {WorkstreamsCard} from './workstreams-card.js';

/** Whether the run is over: ended or failed, so nothing more will come of it. */
const isOver = (status: RunStatus): boolean =>
  status === RunStatus.ENDED || status === RunStatus.FAILED;

/** The side column: the latest measurements, the workstreams, and the agents with their cost. */
function SideColumn(props: {
  readonly report: RunReport;
  readonly over: boolean;
  readonly nowMs: number;
  readonly cost: CostView | undefined;
  readonly costFeed: CostFeedSnapshot;
  readonly retryCost: () => void;
  readonly openAgents: () => void;
  readonly openCost: () => void;
}): JSX.Element {
  const {report} = props;
  return (
    <>
      <LatestMeasurements
        rows={report.latest}
        objectives={report.objectives}
        nowMs={props.nowMs}
        latestMs={report.facts.measured.latestMs}
        runOver={props.over}
      />
      {report.plans === undefined ? null : <PlansSummary plans={report.plans} />}
      <WorkstreamsCard workstreams={report.ideas?.workstreams ?? []} runOver={props.over} />
      <AgentsAndCost
        agents={report.agents}
        cost={props.cost}
        feed={props.costFeed}
        retry={props.retryCost}
        openAgents={props.openAgents}
        open={props.openCost}
        runOver={props.over}
      />
    </>
  );
}

/** Everything the report reads and every intent its controls can send. */
interface ReportScreenProps {
  readonly report: RunReport;
  /** The run's Settings control, in the title row beside its actions; none for a run the host does not hold. */
  readonly settings?: ReactNode;
  readonly host: string;
  readonly nowMs: number;
  /** The run's cost, as the host last sent it, and how its stream is going. */
  readonly cost: CostView | undefined;
  readonly costFeed: CostFeedSnapshot;
  readonly retryCost: () => void;
  readonly stopProgress: CommandProgress | undefined;
  readonly stop: () => void;
  readonly retryStop: () => void;
  /** Open the run's Catch me up page; opening it makes no check-in. */
  readonly openCatchUp: () => void;
  /** Leave an over run for a fresh New run. */
  readonly runAgain: () => void;
  readonly goRuns: () => void;
  readonly openAgents: () => void;
  readonly openCost: () => void;
  /** What each agent is called on screen, by agent id. */
  readonly names: ReadonlyMap<string, string>;
  /** Where the read of the run's history stands, and how the host's link stands, among the state words beside the title. */
  readonly reading: ReactNode;
  /** The check-in span the ideas are narrowed to on arrival (from Catch me up); null for the run's own. */
  readonly ideasWindow: IdeaWindow | null;
}

/** Settings and state-changing controls in the report header, in their reading order. */
function ReportHeaderActions(props: ReportScreenProps): JSX.Element {
  const over = isOver(props.report.header.status);
  return (
    <>
      {props.settings}
      <ReportActions
        stoppable={isStoppable(props.report.header.status)}
        repeatable={over}
        stopProgress={props.stopProgress}
        stop={props.stop}
        openCatchUp={props.openCatchUp}
        runAgain={props.runAgain}
      />
    </>
  );
}

/**
 * A run's bird's-eye report, as the page a run opens on: who and how it stands (its title row and the
 * facts line under it), what the lead says in one row, and then two columns. The main column is how
 * each objective is doing over time and the run's ideas; the side column is the latest measurements,
 * the workstreams, and the agents with their cost. The layout is one for every state: every panel is
 * in its place from the first minute, an empty one shows its frame and says in a line what it waits
 * for, and none moves as the run grows. It shows no agent's turns: the agents and the cost each open
 * their own screen. A point of a chart links to the idea it measured and back.
 */
export function ReportScreen(props: ReportScreenProps): JSX.Element {
  const {report} = props;
  const over = isOver(report.header.status);
  const [picked, setPicked] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [reveal, setReveal] = useState(0);
  const ideas = report.ideas;
  const title = runTitle(report.header.card, report.header.name);
  const lit = new Set(ideas?.ideas.find(idea => idea.planNumber === selected)?.candidateJobIds);
  const units = new Map(report.objectives.map(objective => [objective.name, objective.unit]));
  return (
    <Page
      key="report"
      width="fill"
      state="ready"
      trail={[
        {id: 'runs', label: 'Runs', press: props.goRuns},
        {id: report.runId, label: title},
      ]}
      title={title}
      meta={
        <Stack gap="space-2">
          <Row gap="space-4" rowGap="space-2" wrap>
            <ReportMeta report={report} host={props.host} nowMs={props.nowMs} />
            {props.reading}
          </Row>
          <FactsLine facts={report.facts} over={over} />
        </Stack>
      }
      actions={<ReportHeaderActions {...props} />}
    >
      <Stack gap="space-4">
        <CommandLine
          progress={over ? undefined : props.stopProgress}
          done="Stop run"
          retry={props.retryStop}
        />
        <LeadSummary
          summary={report.summary}
          nowMs={props.nowMs}
          over={over}
          openCatchUp={props.openCatchUp}
        />
        <MainSide
          label="The report"
          main={
            <>
              <PerformancePanel
                objectives={report.objectives}
                span={report.span}
                nowMs={props.nowMs}
                runOver={over}
                unreadable={report.search.state === 'unreadable'}
                truncated={report.truncated}
                names={props.names}
                links={{
                  ideas,
                  picked,
                  lit,
                  pick: jobId => setPicked(jobId === picked ? null : jobId),
                  showIdea: plan => {
                    setSelected(plan);
                    setReveal(reveal + 1);
                  },
                }}
              />
              <IdeasPanel
                ideas={ideas}
                names={props.names}
                units={units}
                nowMs={props.nowMs}
                window={props.ideasWindow}
                selected={selected}
                select={setSelected}
                reveal={reveal}
                runOver={over}
              />
              {report.tradeoff === undefined ? null : (
                <TradeOff tradeoff={report.tradeoff} objectives={report.objectives} />
              )}
            </>
          }
          side={
            <SideColumn
              report={report}
              over={over}
              nowMs={props.nowMs}
              cost={props.cost}
              costFeed={props.costFeed}
              retryCost={props.retryCost}
              openAgents={props.openAgents}
              openCost={props.openCost}
            />
          }
        />
      </Stack>
    </Page>
  );
}
