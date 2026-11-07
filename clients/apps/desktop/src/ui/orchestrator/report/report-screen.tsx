import {RunStatus} from '@vibesys/orchestrator-proto';
import {ChartColumns, Page} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import type {
  CommandProgress,
  CostFeedSnapshot,
  CostView,
  Freshness,
  RunReport,
} from '../../../state/index.js';
import {CommandLine} from '../command-line.js';
import {AgentsCard} from './agents-card.js';
import {CostCard} from './cost-card.js';
import {LeadSummary} from './lead-summary.js';
import {ObjectiveTiles} from './objective-tiles.js';
import {OverTime, TradeOff} from './performance.js';
import {isStoppable, ReportActions, ReportMeta} from './report-title.js';

/**
 * A run's bird's-eye report, as the page a run opens on: who and how it stands (its title row), what
 * the lead says, how each objective is doing, the candidates measured over time and against each
 * other, the agents by type and what it costs. It shows no agent's turns: the agents and the cost
 * each open their own screen. `ideas` is the place reserved for the run's ideas and workstreams,
 * drawn only when it is given.
 */
export function ReportScreen(props: {
  readonly report: RunReport;
  readonly host: string;
  readonly nowMs: number;
  /** How current the run stream is; the header says so when it is not. */
  readonly freshness: Freshness;
  /** How long what is shown has been out of date; null while it is current. */
  readonly staleForMs: number | null;
  /** The run's cost, as the host last sent it, and how its stream is going. */
  readonly cost: CostView | undefined;
  readonly costFeed: CostFeedSnapshot;
  readonly retryCost: () => void;
  readonly stopProgress: CommandProgress | undefined;
  readonly stop: () => void;
  readonly retryStop: () => void;
  /** Press Catch me up on the run and open its page. */
  readonly catchUp: () => void;
  readonly goRuns: () => void;
  readonly openAgents: () => void;
  readonly openCost: () => void;
  /** What each agent is called on screen, by agent id. */
  readonly names: ReadonlyMap<string, string>;
  /** The state of every connection that is not working, above the report; nothing when all work. */
  readonly notice: ReactNode;
  readonly ideas?: ReactNode;
}): JSX.Element {
  const {report} = props;
  const over =
    report.header.status === RunStatus.ENDED || report.header.status === RunStatus.FAILED;
  return (
    <Page
      key="report"
      width="wide"
      state="ready"
      trail={[
        {id: 'runs', label: 'Runs', press: props.goRuns},
        {id: report.runId, label: report.header.name},
      ]}
      title={report.header.name}
      meta={
        <ReportMeta
          report={report}
          host={props.host}
          freshness={props.freshness}
          nowMs={props.nowMs}
          staleForMs={props.staleForMs}
        />
      }
      actions={
        <ReportActions
          stoppable={isStoppable(report.header.status)}
          stopProgress={props.stopProgress}
          stop={props.stop}
          catchUp={props.catchUp}
        />
      }
    >
      {props.notice}
      <CommandLine progress={props.stopProgress} done="Stop" retry={props.retryStop} />
      <LeadSummary summary={report.summary} nowMs={props.nowMs} />
      <ObjectiveTiles objectives={report.objectives} />
      <ChartColumns>
        {report.tradeoff === undefined ? null : (
          <TradeOff tradeoff={report.tradeoff} objectives={report.objectives} />
        )}
        <OverTime objectives={report.objectives} span={report.span} names={props.names} />
      </ChartColumns>
      {props.ideas ?? null}
      <ChartColumns align="start">
        <AgentsCard agents={report.agents} runOver={over} open={props.openAgents} />
        <CostCard
          cost={props.cost}
          feed={props.costFeed}
          runOver={over}
          retry={props.retryCost}
          open={props.openCost}
        />
      </ChartColumns>
    </Page>
  );
}
