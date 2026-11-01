import {Button, Text} from '@vibesys/ui';
import type {JSX} from 'react';

export interface RelaunchView {
  readonly state: 'ready' | 'launching' | 'settled' | 'uncertain' | 'unavailable';
  readonly result: string | null;
  readonly appliedEpoch: bigint | null;
  readonly available: boolean;
  readonly launch: () => void;
}
export function RelaunchAction(props: RelaunchView): JSX.Element | null {
  if (!props.available && props.state !== 'launching') return null;
  return (
    <Button
      text="Resume"
      size="sm"
      variant="secondary"
      quietReason
      press={props.launch}
      {...(props.state !== 'ready'
        ? {
            disabledReason:
              props.state === 'launching'
                ? 'Waiting for the host'
                : props.state === 'unavailable'
                  ? 'Resume unavailable'
                  : 'Refresh the run before resuming again',
          }
        : {})}
    />
  );
}
export function RelaunchStatus(
  props: RelaunchView & {readonly epoch: bigint; readonly earlierAction: boolean},
): JSX.Element | null {
  if (props.result)
    return (
      <Text title={props.result}>
        Resume · {props.appliedEpoch !== null ? 'Applied' : props.result}
        {props.appliedEpoch !== null && props.appliedEpoch < props.epoch
          ? ' · Earlier epoch'
          : props.earlierAction
            ? ' · Earlier action'
            : ''}
      </Text>
    );
  if (props.state === 'launching') return <Text tone="muted">Waiting for the host</Text>;
  if (props.state === 'uncertain')
    return <Text tone="warn">Resume not confirmed. Check the timeline before trying again.</Text>;
  return null;
}
