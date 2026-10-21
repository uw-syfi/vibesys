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
      text="Relaunch"
      variant="secondary"
      quietReason
      press={props.launch}
      {...(props.state !== 'ready'
        ? {
            disabledReason:
              props.state === 'launching'
                ? 'Waiting for the host'
                : props.state === 'unavailable'
                  ? 'Relaunch unavailable'
                  : 'Refresh the run before relaunching again',
          }
        : {})}
    />
  );
}
export function RelaunchStatus(props: RelaunchView): JSX.Element | null {
  if (props.result) return <Text>{props.result}</Text>;
  if (props.state === 'launching') return <Text tone="muted">Waiting for the host</Text>;
  if (props.state === 'uncertain')
    return <Text tone="warn">Relaunch not confirmed. Check the timeline before trying again.</Text>;
  return props.state === 'ready' ? (
    <Text tone="muted">Update the host checkout before relaunching.</Text>
  ) : null;
}
