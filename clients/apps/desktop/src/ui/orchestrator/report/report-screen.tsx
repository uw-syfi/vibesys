import {Row, Stack, Text} from '@vibesys/ui';
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
  /** The state of the cost stream the report follows while it is shown. */
  readonly costFeed: FeedStatus;
  readonly stopProgress: CommandProgress | undefined;
  readonly stop: () => void;
  readonly retryStop: () => void;
  readonly goRuns: () => void;
  readonly openAgents: () => void;
  readonly openCost: () => void;
  readonly ideas?: ReactNode;
}): JSX.Element {
  const {report} = props;
  return (
    <Stack gap="space-5">
      <ReportHeader
        report={report}
        host={props.host}
        freshness={props.freshness}
        stoppable={isStoppable(report.header.status)}
        stopProgress={props.stopProgress}
        stop={props.stop}
        retryStop={props.retryStop}
        goRuns={props.goRuns}
      />
      {report.search.state === 'unreadable' ? (
        <Row gap="space-3" wrap>
          <Text tone="warn">Search state is stale</Text>
        </Row>
      ) : null}
      <LeadSummary summary={report.summary} nowMs={props.nowMs} />
      <ObjectiveTiles objectives={report.objectives} />
      <Row gap="space-5" wrap align="start">
        {report.tradeoff === undefined ? null : (
          <Stack width="chart" gap="space-4">
            <TradeOff tradeoff={report.tradeoff} objectives={report.objectives} />
          </Stack>
        )}
        <Stack width="chart" gap="space-4">
          <OverTime objectives={report.objectives} span={report.span} />
        </Stack>
      </Row>
      {props.ideas ?? null}
      <Row gap="space-5" wrap align="start">
        <Stack width="chart" gap="space-4">
          <AgentsCard agents={report.agents} open={props.openAgents} />
        </Stack>
        <Stack width="chart" gap="space-4">
          <CostCard cost={report.cost} feed={props.costFeed} open={props.openCost} />
        </Stack>
      </Row>
    </Stack>
  );
}
