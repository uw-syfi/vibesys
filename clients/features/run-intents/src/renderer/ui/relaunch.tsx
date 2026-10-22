import {Button, Text} from '@vibesys/ui';
import type {JSX} from 'react';

export interface RelaunchView {
  readonly state: 'ready' | 'launching' | 'settled' | 'uncertain' | 'unavailable';
  readonly result: string | null;
  readonly launch: () => void;
}
export function RelaunchAction(props: RelaunchView): JSX.Element {
  return (
    <Button
      text="Resume"
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
export function RelaunchStatus(props: RelaunchView): JSX.Element | null {
  if (props.result) return <Text>{props.result}</Text>;
  if (props.state === 'launching') return <Text tone="muted">Waiting for the host</Text>;
  if (props.state === 'uncertain')
    return <Text tone="warn">Resume not confirmed. Check the timeline before trying again.</Text>;
  return null;
}
