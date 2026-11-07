import {RunStatus} from '@vibesys/orchestrator-proto';
import {Breadcrumb, Button, Chip, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CommandProgress, Freshness, ObjectiveReport, RunReport} from '../../../state/index.js';
import {CommandLine} from '../command-line.js';
import {runWord} from '../words.js';
import {formatAge, formatElapsed, formatFigure, seattleMoment, withUnit} from './format.js';

/** What a data state is called when the data is not current; a current one needs no word. */
const FRESHNESS: Readonly<
  Record<Exclude<Freshness, 'live'>, {label: string; tone: 'neutral' | 'warn'; hint: string}>
> = {
  'catching-up': {label: 'Catching up', tone: 'neutral', hint: 'Reading what changed.'},
  stale: {label: 'Stale', tone: 'warn', hint: 'The connection is down. What is shown is older.'},
  empty: {label: 'Connecting', tone: 'neutral', hint: 'Nothing has been read yet.'},
  unavailable: {label: 'Unavailable', tone: 'warn', hint: 'Nothing can be read until you act.'},
};

/** What the objective's best is, as it reads on one line: its value and, when served, how close to its bound. */
function BestLine(props: {readonly objectives: readonly ObjectiveReport[]}): JSX.Element | null {
  const rows = props.objectives.flatMap(objective => {
    const value = objective.headValue ?? objective.best?.value;
    return value === undefined ? [] : [{objective, value}];
  });
  if (rows.length === 0) return null;
  return (
    <Row gap="space-5" rowGap="space-1" wrap align="baseline">
      <Text variant="small" tone="muted">
        Best
      </Text>
      {rows.map(({objective, value}) => (
        <Row key={objective.name} gap="space-2" align="baseline">
          <Text variant="metric">{withUnit(formatFigure(value), objective.unit)}</Text>
          <Text variant="small" tone="muted" title={objective.name}>
            {objective.percentOfBound === undefined
              ? objective.name
              : `${objective.name} · ${Math.round(objective.percentOfBound)}% of bound`}
          </Text>
        </Row>
      ))}
    </Row>
  );
}

/**
 * The top of the report: where the run is (its name, state, host, how long it has run and when it
 * began, in Seattle time), what is best so far on each objective, and the Stop that ends it. The
 * state chip is the run's own; a second chip says when what is shown is not current.
 */
export function ReportHeader(props: {
  readonly report: RunReport;
  readonly host: string;
  readonly freshness: Freshness;
  readonly nowMs: number;
  readonly staleForMs: number | null;
  readonly stoppable: boolean;
  readonly stopProgress: CommandProgress | undefined;
  readonly stop: () => void;
  readonly retryStop: () => void;
  readonly goRuns: () => void;
}): JSX.Element {
  const {header, objectives} = props.report;
  const status = runWord(header.status);
  const stale = props.freshness === 'live' ? undefined : FRESHNESS[props.freshness];
  const staleHint =
    stale === undefined
      ? undefined
      : props.staleForMs === null
        ? stale.hint
        : `${stale.hint} Last current ${formatAge(props.staleForMs)}.`;
  return (
    <Stack gap="space-3">
      <Breadcrumb
        label="Where you are"
        items={[
          {id: 'runs', label: 'Runs', press: props.goRuns},
          {id: props.report.runId, label: header.name},
        ]}
      />
      <Row gap="space-4" rowGap="space-3" wrap justify="between" align="start">
        <Stack gap="space-3">
          <BestLine objectives={objectives} />
          <Row gap="space-3" rowGap="space-2" wrap>
            <Chip label={status.label} tone={status.tone} dot />
            {stale === undefined ? null : (
              <Chip
                label={stale.label}
                tone={stale.tone}
                {...(staleHint === undefined ? {} : {hint: staleHint})}
              />
            )}
            {props.report.search.state === 'unreadable' ? (
              <Chip
                label="Search stale"
                tone="warn"
                hint={`The search service was lost ${formatAge(props.report.search.sinceMs === 0 ? 0 : props.nowMs - props.report.search.sinceMs)}. The measurements shown were the last read.`}
              />
            ) : null}
            <Chip label={props.host} mono />
            <Text variant="small" title={header.endedMs === null ? 'Running for' : 'Ran for'}>
              {formatElapsed(header.elapsedMs)}
            </Text>
            <Text variant="small" tone="muted">
              {`Started ${seattleMoment(header.startedMs)}`}
            </Text>
          </Row>
        </Stack>
        {props.stoppable ? (
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
    </Stack>
  );
}

/** Whether a run can still be stopped. */
export const isStoppable = (status: RunStatus): boolean =>
  status === RunStatus.STARTING || status === RunStatus.RUNNING;
