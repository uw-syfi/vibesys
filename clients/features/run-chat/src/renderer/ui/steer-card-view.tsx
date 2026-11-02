import {
  Button,
  Card,
  Chip,
  Field,
  Row,
  SegmentedControl,
  Select,
  Stack,
  Text,
  TextInput,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type CardActions,
  type CardView,
  parseTargetValue,
  STEER_MODES,
  type SteerDraft,
  targetOptions,
  targetValue,
} from '../../state/index.js';
import {FailureNote} from './failure-note.js';

interface CardProps {
  readonly draft: SteerDraft;
  readonly view: CardView;
  readonly actions: CardActions;
  /** The run's roles: what the target select lists after the whole run. */
  readonly roles: readonly string[];
}

function Draft(props: CardProps): JSX.Element {
  const {draft, view, actions, roles} = props;
  return (
    <Stack gap="space-3">
      <Field label="Steer text" hideLabel>
        <TextInput value={draft.text} rows={3} change={actions.editText} />
      </Field>
      <Row gap="space-3" wrap>
        <Field label="To" hideLabel>
          <Select
            size="sm"
            value={targetValue(draft.target)}
            options={targetOptions(roles, draft.target)}
            change={value => actions.setTarget(parseTargetValue(value))}
          />
        </Field>
        <SegmentedControl
          label="When"
          size="xs"
          options={STEER_MODES}
          value={draft.mode}
          change={actions.setMode}
        />
      </Row>
      {view.error === null ? null : <FailureNote failure={view.error} />}
      <Row gap="space-3" align="start">
        <Button
          variant="primary"
          size="sm"
          text="Send"
          press={actions.send}
          {...(view.sendBlocked === null ? {} : {disabledReason: view.sendBlocked})}
        />
        <Button variant="quiet" size="sm" text="Dismiss" press={actions.dismiss} />
      </Row>
    </Stack>
  );
}

/**
 * One steer, the only bordered thing in the panel: editable until Send, then its text and one
 * status line. Nothing reaches the run until Send is pressed; a standing steer in force offers
 * Withdraw.
 */
export function SteerCardView(props: CardProps): JSX.Element | null {
  const {draft, view, actions} = props;
  if (view.phase === 'dismissed') return null;
  return (
    <Card>
      {view.phase === 'draft' ? (
        <Draft {...props} />
      ) : (
        <Stack gap="space-3">
          <Text>{draft.text}</Text>
          <Row gap="space-3" justify="between" wrap>
            {view.status === null ? null : (
              <Chip label={view.status.label} tone={view.status.tone} dot />
            )}
            {view.withdrawable ? (
              <Button variant="quiet" size="sm" text="Withdraw" press={actions.withdraw} />
            ) : null}
          </Row>
          {view.error === null ? null : <FailureNote failure={view.error} />}
        </Stack>
      )}
    </Card>
  );
}
