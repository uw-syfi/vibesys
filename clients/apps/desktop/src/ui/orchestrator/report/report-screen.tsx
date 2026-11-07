import {ChartColumns, Stack} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import type {CommandProgress, FeedStatus, Freshness, RunReport} from '../../../state/index.js';
import {AgentsCard} from './agents-card.js';
import {CostCard} from './cost-card.js';
import {LeadSummary} from './lead-summary.js';
import {ObjectiveTiles} from './objective-tiles.js';
import {OverTime, TradeOff} from './performance.js';
import {isStoppable, ReportHeader} from './report-header.js';

/**
 * A run's bird's-eye report: who and how it stands, what the lead says, how each objective is doing,
 * the candidates measured over time and against each other, the agents by type and what it costs.
 * It shows no agent's turns: the agents and the cost each open their own screen. `ideas` is the place
 * reserved for the run's ideas and workstreams, drawn only when it is given.
 */
export function ReportScreen(props: {
  readonly report: RunReport;
  readonly host: string;
  readonly nowMs: number;
  /** How current the run stream is; the header says so when it is not. */
  readonly freshness: Freshness;
  /** How long what is shown has been out of date; null while it is current. */
  readonly staleForMs: number | null;
  /** The state of the cost stream the report follows while it is shown. */
  readonly costFeed: FeedStatus;
  readonly stopProgress: CommandProgress | undefined;
  readonly stop: () => void;
  readonly retryStop: () => void;
  readonly goRuns: () => void;
  readonly openAgents: () => void;
  readonly openCost: () => void;
  /** What each agent is called on screen, by agent id. */
  readonly names: ReadonlyMap<string, string>;
  readonly ideas?: ReactNode;
}): JSX.Element {
  const {report} = props;
  return (
    <Stack gap="space-5">
      <ReportHeader
        report={report}
        host={props.host}
        freshness={props.freshness}
        nowMs={props.nowMs}
        staleForMs={props.staleForMs}
        stoppable={isStoppable(report.header.status)}
        stopProgress={props.stopProgress}
        stop={props.stop}
        retryStop={props.retryStop}
        goRuns={props.goRuns}
      />
      <LeadSummary summary={report.summary} nowMs={props.nowMs} />
      <ObjectiveTiles objectives={report.objectives} />
      <ChartColumns>
        {report.tradeoff === undefined ? null : (
          <TradeOff tradeoff={report.tradeoff} objectives={report.objectives} />
        )}
        <OverTime objectives={report.objectives} span={report.span} names={props.names} />
      </ChartColumns>
      {props.ideas ?? null}
      <ChartColumns>
        <AgentsCard agents={report.agents} open={props.openAgents} />
        <CostCard cost={report.cost} feed={props.costFeed} open={props.openCost} />
      </ChartColumns>
    </Stack>
  );
}
