import {IntentAction, type RunIntentAction} from '@vibesys/runs/intents';
import {Button, Dialog, Field, Row, Stack, Text, TextInput} from '@vibesys/ui';
import type {JSX} from 'react';
import type {IntentRow} from '../../state/index.js';
import {RelaunchAction, RelaunchStatus, type RelaunchView} from './relaunch.js';

const ACTION_LABELS: Record<IntentAction, string> = {
  [IntentAction.UNSPECIFIED]: 'Request',
  [IntentAction.PAUSE]: 'Pause',
  [IntentAction.PARK]: 'Pause',
  [IntentAction.PAUSE_NOW]: 'Pause now',
  [IntentAction.RESUME]: 'Resume',
  [IntentAction.STOP]: 'Stop',
  [IntentAction.STEER]: 'Guidance',
  [IntentAction.WITHDRAW]: 'Withdraw guidance',
  [IntentAction.CANCEL_ATTEMPT]: 'Cancel attempt',
  [IntentAction.CANCEL_ITEM]: 'Cancel item',
  [IntentAction.SET_MAX_PARALLEL]: 'Change parallelism',
};
const BUTTON_LABELS: Record<RunIntentAction, string> = {
  pause: 'Pause',
  pauseNow: 'Pause now',
  steer: 'Guidance',
};
const STATUS_LABELS: Record<IntentRow['status'], string> = {
  sending: 'Sending',
  pending: 'Pending',
  applied: 'Applied',
  refused: 'Refused',
  uncertain: 'Not confirmed',
};

export function IntentControls(props: {
  readonly status?: 'ready' | 'catching-up' | 'unknown' | 'unknown-project';
  readonly busy: boolean;
  readonly actions: readonly RunIntentAction[];
  readonly epoch: bigint;
  readonly relaunch?: RelaunchView;
  readonly entries: readonly IntentRow[];
  readonly error: string | null;
  readonly warning: string | null;
  readonly guidanceOpen: boolean;
  readonly showGuidance: (open: boolean) => void;
  readonly text: string;
  readonly edit: (text: string) => void;
  readonly send: (action: RunIntentAction) => void;
  readonly retry: (requestId: string) => void;
}): JSX.Element {
  return (
    <Stack gap="space-1">
      <WriterStatus status={props.status ?? 'ready'} />
      <Row gap="space-2" wrap>
        {props.actions
          .filter(action => action !== 'steer')
          .map(action => (
            <Button
              key={action}
              variant={action === 'pauseNow' ? 'danger' : 'secondary'}
              quietReason
              {...(props.busy ? {disabledReason: 'Sending'} : {})}
              press={() => props.send(action)}
              text={BUTTON_LABELS[action]}
            />
          ))}
        {props.relaunch ? <RelaunchAction {...props.relaunch} /> : null}
        {props.actions.includes('steer') ? (
          <Button text="Guidance" press={() => props.showGuidance(true)} />
        ) : null}
      </Row>
      {props.actions.includes('pauseNow') ? (
        <Text tone="muted">Unfinished work may need repeating and may still incur charges.</Text>
      ) : null}
      {props.entries.length > 0 || props.relaunch?.result ? (
        <Text variant="small" tone="muted">
          Request history
        </Text>
      ) : null}
      {props.relaunch &&
      !props.entries.some(
        entry =>
          entry.action === IntentAction.RESUME &&
          entry.status === 'applied' &&
          entry.epoch === props.relaunch?.appliedEpoch,
      ) ? (
        <RelaunchStatus
          {...props.relaunch}
          epoch={props.epoch}
          earlierAction={props.entries.some(
            entry => entry.status === 'applied' && entry.epoch === props.relaunch?.appliedEpoch,
          )}
        />
      ) : null}
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
        <Row key={`${entry.source}:${entry.id}`} gap="space-2" wrap>
          <Text {...(entry.reason ? {title: entry.reason} : {})}>
            {ACTION_LABELS[entry.action]} · {STATUS_LABELS[entry.status]}
            {entry.epoch !== null && entry.epoch < props.epoch ? ' · Earlier epoch' : ''}
            {entry.epoch === props.epoch && entry.status === 'applied' && !entry.current
              ? ' · Earlier action'
              : ''}
          </Text>
          {entry.status === 'uncertain' ? (
            <Button
              {...(props.busy ? {disabledReason: 'Sending'} : {})}
              press={() => props.retry(entry.id)}
              text="Retry request"
            />
          ) : null}
        </Row>
      ))}
      <Dialog
        open={props.guidanceOpen && props.actions.includes('steer')}
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

function WriterStatus({
  status,
}: {
  readonly status: 'ready' | 'catching-up' | 'unknown' | 'unknown-project';
}) {
  const words = {
    ready: null,
    'unknown-project': 'Project unknown',
    'catching-up': 'Catching up with the run… Reconnecting automatically.',
    unknown: 'Run status unknown. Reconnecting automatically.',
  };
  return words[status] ? <Text tone="muted">{words[status]}</Text> : null;
}
