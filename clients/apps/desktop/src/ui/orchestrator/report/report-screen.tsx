import {RunStatus} from '@vibesys/orchestrator-proto';
import {ChartColumns, Page, Stack} from '@vibesys/ui';
import {type JSX, type ReactNode, useState} from 'react';
import type {
  CommandProgress,
  CostFeedSnapshot,
  CostView,
  HostStatus,
  IdeaWindow,
  RunReport,
} from '../../../state/index.js';
import {CommandLine} from '../command-line.js';
import {runTitle} from '../fleet-words.js';
import {AgentsCard} from './agents-card.js';
import {CostCard} from './cost-card.js';
import {IdeasCard} from './ideas-card.js';
import {LeadSummary} from './lead-summary.js';
import {ObjectiveTiles} from './objective-tiles.js';
import {OverTime, TradeOff} from './performance.js';
import {PlansSummary} from './plans-summary.js';
import {isStoppable, ReportActions, ReportMeta} from './report-title.js';
import {WorkstreamsCard} from './workstreams-card.js';

/**
 * A run's bird's-eye report, as the page a run opens on: who and how it stands (its title row), what
 * the lead says, how each objective is doing, the candidates measured over time and against each
 * other, the agents by type and what it costs. It shows no agent's turns: the agents and the cost
 * each open their own screen. The run's workstreams and ideas follow the charts once a search state
 * is served; a point of a chart links to the idea it measured and back.
 */
export function ReportScreen(props: {
  readonly report: RunReport;
  /** The run's Settings control, in the title row beside its actions; none for a run the host does not hold. */
  readonly settings?: ReactNode;
  readonly host: string;
  readonly nowMs: number;
  /** The host's one status; the header says so when it is not live. */
  readonly status: HostStatus;
  /** The run's cost, as the host last sent it, and how its stream is going. */
  readonly cost: CostView | undefined;
  readonly costFeed: CostFeedSnapshot;
  readonly retryCost: () => void;
  readonly stopProgress: CommandProgress | undefined;
  readonly stop: () => void;
  readonly retryStop: () => void;
  /** Open the run's Catch me up page; opening it makes no check-in. */
  readonly openCatchUp: () => void;
  readonly goRuns: () => void;
  readonly openAgents: () => void;
  readonly openCost: () => void;
  /** What each agent is called on screen, by agent id. */
  readonly names: ReadonlyMap<string, string>;
  /** Where the read of the run's history stands, among the state words beside the title. */
  readonly reading: ReactNode;
  /** The check-in span the ideas are narrowed to on arrival (from Catch me up); null for the run's own. */
  readonly ideasWindow: IdeaWindow | null;
}): JSX.Element {
  const {report} = props;
  const over =
    report.header.status === RunStatus.ENDED || report.header.status === RunStatus.FAILED;
  const [picked, setPicked] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [reveal, setReveal] = useState(0);
  const ideas = report.ideas;
  const title = runTitle(report.header.card, report.header.name);
  const lit = new Set(ideas?.ideas.find(idea => idea.planNumber === selected)?.candidateJobIds);
  const units = new Map(report.objectives.map(objective => [objective.name, objective.unit]));
  const cost = (
    <CostCard
      cost={props.cost}
      feed={props.costFeed}
      runOver={over}
      retry={props.retryCost}
      open={props.openCost}
    />
  );
  return (
    <Page
      key="report"
      width="wide"
      state="ready"
      trail={[
        {id: 'runs', label: 'Runs', press: props.goRuns},
        {id: report.runId, label: title},
      ]}
      title={title}
      meta={
        <>
          <ReportMeta report={report} host={props.host} status={props.status} nowMs={props.nowMs} />
          {props.reading}
        </>
      }
      actions={
        <>
          {props.settings}
          <ReportActions
            stoppable={isStoppable(report.header.status)}
            stopProgress={props.stopProgress}
            stop={props.stop}
            openCatchUp={props.openCatchUp}
          />
        </>
      }
    >
      <Stack>
        <CommandLine
          progress={over ? undefined : props.stopProgress}
          done="Stop"
          retry={props.retryStop}
        />
        <LeadSummary summary={report.summary} nowMs={props.nowMs} />
        <ObjectiveTiles objectives={report.objectives} />
        <ChartColumns dense>
          {report.tradeoff === undefined ? null : (
            <TradeOff tradeoff={report.tradeoff} objectives={report.objectives} />
          )}
          <OverTime
            objectives={report.objectives}
            span={report.span}
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
        </ChartColumns>
        {ideas === undefined ? (
          <ChartColumns dense align="start">
            <AgentsCard agents={report.agents} runOver={over} open={props.openAgents} />
            {cost}
          </ChartColumns>
        ) : (
          <>
            {report.plans === undefined ? null : <PlansSummary plans={report.plans} />}
            <ChartColumns dense align="start">
              <WorkstreamsCard workstreams={ideas.workstreams} />
              {cost}
            </ChartColumns>
            <IdeasCard
              ideas={ideas}
              names={props.names}
              units={units}
              window={props.ideasWindow}
              selected={selected}
              select={setSelected}
              reveal={reveal}
            />
            <AgentsCard agents={report.agents} runOver={over} open={props.openAgents} />
          </>
        )}
      </Stack>
    </Page>
  );
}
