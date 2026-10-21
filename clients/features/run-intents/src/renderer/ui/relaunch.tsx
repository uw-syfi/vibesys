import {Button, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';

export function RelaunchControl(props: {
  readonly state: 'ready' | 'launching' | 'settled' | 'uncertain';
  readonly result: string | null;
  readonly launch: () => void;
}): JSX.Element {
  return (
    <Stack gap="space-2">
      <Button
        text={props.state === 'launching' ? 'Relaunching' : 'Relaunch'}
        press={props.launch}
        {...(props.state !== 'ready'
          ? {disabledReason: 'Refresh the run before relaunching again'}
          : {})}
      />
      {props.result ? <Text>{props.result}</Text> : null}
      {props.state === 'uncertain' ? (
        <Text tone="warn">Relaunch not confirmed. Check the timeline before trying again.</Text>
      ) : null}
      <Text tone="muted">Update the host checkout before relaunching.</Text>
    </Stack>
  );
}
