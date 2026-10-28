import {IntentAction, type RunIntentAction} from '@vibesys/runs/intents';
import {Button, Dialog, ErrorActions, Field, Row, Stack, Text, TextInput} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  ACTION_LABELS,
  type ErrorReport,
  type IntentRow,
  refusalReport,
  shownReason,
  updatesReport,
} from '../../state/index.js';
import {RelaunchAction, RelaunchStatus, type RelaunchView} from './relaunch.js';

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
  /** Hands an error to the run chat; absent where no chat can take it. */
  readonly askAboutError?: ((report: ErrorReport) => void) | undefined;
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
      <Notices
        warning={props.warning}
        error={props.error}
        entries={props.entries}
        askAboutError={props.askAboutError}
      />
      {props.entries.map(entry => (
        <RequestRow
          key={`${entry.source}:${entry.id}`}
          entry={entry}
          epoch={props.epoch}
          busy={props.busy}
          retry={props.retry}
          askAboutError={props.askAboutError}
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

/**
 * Why the history may be incomplete or out of order, each with what the host said. An unconfirmed
 * post's reason is the error too; its row already says it, with its own offer, so it is said once.
 */
function Notices(props: {
  readonly warning: string | null;
  readonly error: string | null;
  readonly entries: readonly IntentRow[];
  readonly askAboutError?: ((report: ErrorReport) => void) | undefined;
}): JSX.Element {
  const {warning, askAboutError: ask} = props;
  const error = props.entries.some(entry => shownReason(entry) === props.error)
    ? null
    : props.error;
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
          <Text variant="small" tone="muted">
            {error}
          </Text>
          {ask === undefined ? null : (
            <Row gap="space-2">
              <ErrorActions ask={() => ask(updatesReport(error))} />
            </Row>
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
  readonly askAboutError?: ((report: ErrorReport) => void) | undefined;
}): JSX.Element {
  const {entry, askAboutError: ask} = props;
  const reason = shownReason(entry);
  const report = refusalReport(entry);
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
        {ask === undefined || report === null ? null : <ErrorActions ask={() => ask(report)} />}
      </Row>
      {reason === null ? null : (
        <Text variant="small" tone="err">
          {reason}
        </Text>
      )}
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
