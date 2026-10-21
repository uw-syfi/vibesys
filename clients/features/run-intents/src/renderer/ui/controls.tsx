import {Button, Dialog, Field, Row, Stack, Text, TextInput} from '@vibesys/ui';
import type {JSX} from 'react';
import type {IntentEntry} from '../../state/index.js';

const ACTION_LABELS: Record<NonNullable<IntentEntry['request']['intent']['case']>, string> = {
  pause: 'Pause',
  park: 'Park',
  resume: 'Resume',
  stop: 'Stop',
  steer: 'Guidance',
  withdraw: 'Withdraw guidance',
  cancelAttempt: 'Cancel attempt',
  cancelItem: 'Cancel item',
  setParallelism: 'Change parallelism',
};
const STATUS_LABELS: Record<IntentEntry['status'], string> = {
  sending: 'Sending',
  pending: 'Pending',
  applied: 'Applied',
  refused: 'Refused',
  uncertain: 'Not confirmed',
};

export function IntentControls(props: {
  readonly busy: boolean;
  readonly entries: readonly IntentEntry[];
  readonly error: string | null;
  readonly warning: string | null;
  readonly guidanceOpen: boolean;
  readonly showGuidance: (open: boolean) => void;
  readonly text: string;
  readonly edit: (text: string) => void;
  readonly send: (action: 'pause' | 'park' | 'resume' | 'stop' | 'steer') => void;
  readonly retry: (requestId: string) => void;
}): JSX.Element {
  return (
    <Stack gap="space-1">
      <Row gap="space-2" wrap>
        {(['pause', 'resume', 'stop', 'park'] as const).map(action => (
          <Button
            key={action}
            variant={action === 'stop' ? 'danger' : 'secondary'}
            quietReason
            {...(props.busy ? {disabledReason: 'Sending'} : {})}
            press={() => props.send(action)}
            text={ACTION_LABELS[action]}
          />
        ))}
        <Button text="Guidance" press={() => props.showGuidance(true)} />
      </Row>
      {props.warning ? (
        <Text tone="warn" title={props.warning}>
          Posting order unknown
        </Text>
      ) : null}
      {props.error ? (
        <Text tone="err" title={props.error}>
          Updates unavailable
        </Text>
      ) : null}
      {props.entries.map(entry => (
        <Row key={entry.request.requestId} gap="space-2" wrap>
          <Text {...(entry.reason ? {title: entry.reason} : {})}>
            {entry.request.intent.case ? ACTION_LABELS[entry.request.intent.case] : 'Request'} ·{' '}
            {STATUS_LABELS[entry.status]}
          </Text>
          {entry.status === 'uncertain' ? (
            <Button
              {...(props.busy ? {disabledReason: 'Sending'} : {})}
              press={() => props.retry(entry.request.requestId)}
              text="Retry request"
            />
          ) : null}
        </Row>
      ))}
      <Dialog
        open={props.guidanceOpen}
        title="Guidance"
        dismiss={() => props.showGuidance(false)}
        actions={
          <>
            <Button text="Cancel" press={() => props.showGuidance(false)} />
            <Button
              {...(props.busy || !props.text.trim()
                ? {disabledReason: props.busy ? 'Sending' : 'Enter guidance'}
                : {})}
              press={() => props.send('steer')}
              text="Send guidance"
            />
          </>
        }
      >
        <Field label="Guidance">
          <TextInput rows={3} value={props.text} change={props.edit} />
        </Field>
      </Dialog>
    </Stack>
  );
}
