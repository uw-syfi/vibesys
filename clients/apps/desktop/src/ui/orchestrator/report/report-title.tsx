import {RunStatus} from '@vibesys/orchestrator-proto';
import {Button, Chip, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CommandProgress, Freshness, RunReport} from '../../../state/index.js';
import {runWord} from '../words.js';
import {formatAge, formatElapsed, seattleMoment} from './format.js';

/** What a data state is called when the data is not current; a current one needs no word. */
const FRESHNESS: Readonly<
  Record<Exclude<Freshness, 'live'>, {label: string; tone: 'neutral' | 'warn'; hint: string}>
> = {
  'catching-up': {label: 'Catching up', tone: 'neutral', hint: 'Reading what changed.'},
  stale: {label: 'Stale', tone: 'warn', hint: 'The connection is down. What is shown is older.'},
  empty: {label: 'Connecting', tone: 'neutral', hint: 'Nothing has been read yet.'},
  unavailable: {label: 'Unavailable', tone: 'warn', hint: 'Nothing can be read until you act.'},
};

/**
 * What the report's title row says about the run: its state, the host it runs on, how long it has
 * run and when it began, in Seattle time. The state chip is the run's own; a second chip says when
 * what is shown is not current. What is best so far is the objective tiles' to say, not this row's.
 */
export function ReportMeta(props: {
  readonly report: RunReport;
  readonly host: string;
  readonly freshness: Freshness;
  readonly nowMs: number;
  readonly staleForMs: number | null;
}): JSX.Element {
  const {header} = props.report;
  const status = runWord(header.status);
  const stale = props.freshness === 'live' ? undefined : FRESHNESS[props.freshness];
  const staleHint =
    stale === undefined
      ? undefined
      : props.staleForMs === null
        ? stale.hint
        : `${stale.hint} Last current ${formatAge(props.staleForMs)}.`;
  return (
    <>
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
      <Chip label={props.host} mono clip />
      <Text variant="small" title={header.endedMs === null ? 'Running for' : 'Ran for'}>
        {formatElapsed(header.elapsedMs)}
      </Text>
      <Text variant="small" tone="muted">
        {`Started ${seattleMoment(header.startedMs)}`}
      </Text>
    </>
  );
}

/**
 * The report's actions as one group in the title row: Catch me up, the run's leading action, and
 * beside it Stop run while the run can be stopped. Both are the standard size; Stop run is drawn as
 * an outline in the danger color, so it is never louder than the action the page leads with.
 */
export function ReportActions(props: {
  readonly stoppable: boolean;
  readonly stopProgress: CommandProgress | undefined;
  readonly stop: () => void;
  /** Open the run's Catch me up page. Opening it only reads: a check-in is made by New check-in there. */
  readonly openCatchUp: () => void;
}): JSX.Element {
  return (
    <>
      <Button text="Catch me up" variant="primary" press={props.openCatchUp} />
      {props.stoppable ? (
        <Button
          text="Stop run"
          label="Stop run"
          variant="danger"
          press={props.stop}
          {...(props.stopProgress?._tag === 'Sending'
            ? {disabledReason: 'Stopping.', quietReason: true}
            : {})}
        />
      ) : null}
    </>
  );
}

/** Whether a run can still be stopped. */
export const isStoppable = (status: RunStatus): boolean =>
  status === RunStatus.STARTING || status === RunStatus.RUNNING;
