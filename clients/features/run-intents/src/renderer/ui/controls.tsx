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
      <Notices warning={props.warning} error={props.error} entries={props.entries} />
      {props.entries.map(entry => (
        <RequestRow
          key={`${entry.source}:${entry.id}`}
          entry={entry}
          epoch={props.epoch}
          busy={props.busy}
          retry={props.retry}
        />
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

/** Why the history may be incomplete or out of order, each with what the host said. */
function Notices(props: {
  readonly warning: string | null;
  readonly error: string | null;
  readonly entries: readonly IntentRow[];
}): JSX.Element {
  const {warning, error} = props;
  return (
    <>
      {warning ? (
        <Stack gap="space-1">
          <Text tone="warn">Posting order unknown</Text>
          <Text variant="small" tone="muted">
            {warning}
          </Text>
        </Stack>
      ) : null}
      {error ? (
        <Stack gap="space-1">
          <Text tone="err">Updates unavailable</Text>
          {/* A failed post's reason is already under its row; say it once. */}
          {props.entries.some(entry => shownReason(entry) === error) ? null : (
            <Text variant="small" tone="muted">
              {error}
            </Text>
          )}
        </Stack>
      ) : null}
    </>
  );
}

/** One request of the history: what it asked, where it got to, and why it stopped there. */
function RequestRow(props: {
  readonly entry: IntentRow;
  readonly epoch: bigint;
  readonly busy: boolean;
  readonly retry: (requestId: string) => void;
}): JSX.Element {
  const {entry} = props;
  const reason = shownReason(entry);
  return (
    <Stack gap="space-1">
      <Row gap="space-2" wrap>
        <Text>
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
      {reason === null ? null : (
        <Text variant="small" tone="err">
          {reason}
        </Text>
      )}
    </Stack>
  );
}

/** The reason a refused or unconfirmed request shows under its row; null for the rest. */
function shownReason(entry: IntentRow): string | null {
  return (entry.status === 'refused' || entry.status === 'uncertain') && entry.reason
    ? entry.reason
    : null;
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
