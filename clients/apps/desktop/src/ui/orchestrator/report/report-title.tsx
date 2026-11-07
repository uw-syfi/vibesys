import {RunStatus} from '@vibesys/orchestrator-proto';
import {Button, Chip, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CommandProgress, HostStatus, RunReport} from '../../../state/index.js';
import {HostStatusChip} from '../../host-status-chip.js';
import {runWord} from '../words.js';
import {formatAge, formatElapsed, seattleMoment} from './format.js';

/**
 * What the report's title row says about the run: its state, the host it runs on, how long it has
 * run and when it began, in Seattle time. The state chip is the run's own; a second chip says when
 * what is shown is not current. What is best so far is the objective tiles' to say, not this row's.
 */
export function ReportMeta(props: {
  readonly report: RunReport;
  readonly host: string;
  readonly status: HostStatus;
  readonly nowMs: number;
}): JSX.Element {
  const {header} = props.report;
  const status = runWord(header.status);
  return (
    <>
      <Chip label={status.label} tone={status.tone} dot />
      {props.status.kind === 'live' ? null : <HostStatusChip status={props.status} />}
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
  /** Press Catch me up on the run and open its page. */
  readonly catchUp: () => void;
}): JSX.Element {
  return (
    <>
      <Button text="Catch me up" variant="primary" press={props.catchUp} />
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
