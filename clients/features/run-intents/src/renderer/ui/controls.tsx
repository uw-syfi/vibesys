import {Button, Card, Field, Row, Stack, Text, TextInput} from '@vibesys/ui';
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
  readonly text: string;
  readonly edit: (text: string) => void;
  readonly send: (action: 'pause' | 'resume' | 'stop' | 'steer') => void;
  readonly retry: (requestId: string) => void;
}): JSX.Element {
  return (
    <Card>
      <Stack gap="space-2">
        <Text>Run controls</Text>
        <Row gap="space-2">
          {(['pause', 'resume', 'stop'] as const).map(action => (
            <Button
              key={action}
              quietReason
              {...(props.busy ? {disabledReason: 'Sending a request'} : {})}
              press={() => props.send(action)}
              text={action === 'pause' ? 'Pause' : action === 'resume' ? 'Resume' : 'Stop'}
            />
          ))}
        </Row>
        <Field label="Guidance">
          <TextInput rows={3} value={props.text} change={props.edit} />
        </Field>
        <Button
          {...(props.busy || !props.text.trim()
            ? {disabledReason: props.busy ? 'Sending a request' : 'Enter guidance'}
            : {})}
          press={() => props.send('steer')}
          text="Send guidance"
        />
        <Text tone="muted">Requests stay pending until the run records their outcome.</Text>
        {props.warning ? <Text tone="warn">{props.warning}</Text> : null}
        {props.error ? (
          <Text tone="err" title={props.error}>
            Request updates unavailable
          </Text>
        ) : null}
        {props.entries.map(entry => (
          <Stack key={entry.request.requestId} gap="space-1">
            <Text {...(entry.reason ? {title: entry.reason} : {})}>
              {entry.request.intent.case ? ACTION_LABELS[entry.request.intent.case] : 'Request'}:{' '}
              {STATUS_LABELS[entry.status]}
              {entry.status === 'refused' && entry.reason ? `: ${entry.reason}` : ''}
            </Text>
            {entry.status === 'uncertain' ? (
              <Button
                {...(props.busy ? {disabledReason: 'Sending a request'} : {})}
                press={() => props.retry(entry.request.requestId)}
                text="Retry request"
              />
            ) : null}
          </Stack>
        ))}
      </Stack>
    </Card>
  );
}
