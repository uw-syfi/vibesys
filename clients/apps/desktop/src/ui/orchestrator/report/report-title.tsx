import {RunStatus} from '@vibesys/orchestrator-proto';
import {Button, Chip, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CommandProgress, HostStatus, RunReport, SearchReading} from '../../../state/index.js';
import {useDisplayClock} from '../../display-clock.js';
import type {DisplayClock} from '../../display-time.js';
import {HostStatusChip} from '../../host-status-chip.js';
import {unreadableWord} from '../search-words.js';
import {runWord} from '../words.js';
import {formatAge, formatElapsed} from './format.js';

type UnreadableSearch = Extract<SearchReading, {readonly state: 'unreadable'}>;

/**
 * The account behind the chip of a search the host cannot read: what its reason means, since when
 * the host has said so (or that it did not say), and the host's own words as Details.
 */
function unreadableHint(search: UnreadableSearch, nowMs: number, clock: DisplayClock): string {
  const since =
    search.sinceMs === null
      ? 'The host did not say since when.'
      : `Since ${clock.reportMoment(search.sinceMs)} (${formatAge(nowMs - search.sinceMs)}).`;
  return [
    unreadableWord(search.reason).hint,
    since,
    search.detail === '' ? '' : `Details: ${search.detail}`,
  ]
    .filter(part => part !== '')
    .join(' ');
}

/** The chip of a search the host cannot read: its reason as the label, the account as the hint. */
function UnreadableChip(props: {
  readonly search: UnreadableSearch;
  readonly nowMs: number;
}): JSX.Element {
  const clock = useDisplayClock();
  const word = unreadableWord(props.search.reason);
  return (
    <Chip
      label={word.label}
      tone={word.tone}
      hint={unreadableHint(props.search, props.nowMs, clock)}
    />
  );
}

/**
 * What the report's title row says about the run: its state, the host it runs on, how long it has
 * run and when it began, in Seattle time. The state chip is the run's own; a second chip says when
 * what is shown is not current, and another says why the host cannot read the run's job log when it
 * cannot. What is best so far is the objective tiles' to say, not this row's.
 */
export function ReportMeta(props: {
  readonly report: RunReport;
  readonly host: string;
  readonly status: HostStatus;
  readonly nowMs: number;
}): JSX.Element {
  const {header} = props.report;
  const clock = useDisplayClock();
  const status = runWord(header.status);
  return (
    <>
      <Chip label={status.label} tone={status.tone} dot />
      {props.status.kind === 'live' ? null : <HostStatusChip status={props.status} />}
      {props.report.search.state === 'unreadable' ? (
        <UnreadableChip search={props.report.search} nowMs={props.nowMs} />
      ) : null}
      <Chip label={props.host} mono clip />
      <Text variant="small" title={header.endedMs === null ? 'Running for' : 'Ran for'}>
        {formatElapsed(header.elapsedMs)}
      </Text>
      <Text variant="small" tone="muted">
        {`Started ${clock.reportMoment(header.startedMs)}`}
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
