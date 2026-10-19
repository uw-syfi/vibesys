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
  STEER_MODES,
  type SteerDraft,
  TARGET_KINDS,
} from '../../state/index.js';

function Draft(props: {
  readonly draft: SteerDraft;
  readonly view: CardView;
  readonly actions: CardActions;
}): JSX.Element {
  const {draft, view, actions} = props;
  const {target} = draft;
  return (
    <Stack gap="space-3">
      <Field label="Steer text" hideLabel>
        <TextInput value={draft.text} rows={3} change={actions.editText} />
      </Field>
      <Row gap="space-3" align="end" wrap>
        <Field label="To">
          <Select
            value={target.kind}
            size="sm"
            options={TARGET_KINDS}
            change={kind => actions.setTarget(kind, kind === 'run' ? null : (target.id ?? ''))}
          />
        </Field>
        {target.kind === 'run' ? null : (
          <Field label={`${target.kind} id`} hideLabel>
            <TextInput
              value={target.id ?? ''}
              mono
              placeholder="id"
              change={id => actions.setTarget(target.kind, id)}
            />
          </Field>
        )}
      </Row>
      <Stack gap="space-2">
        <Text variant="small" tone="muted">
          When
        </Text>
        <SegmentedControl
          label="When"
          size="xs"
          options={STEER_MODES}
          value={draft.mode}
          change={actions.setMode}
        />
      </Stack>
      {view.error === null ? null : (
        <Text variant="small" tone="err">
          {view.error}
        </Text>
      )}
      <Row gap="space-3">
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
 * One steer: editable until Send, then its text, where it goes and where it stands. Nothing
 * reaches the run until Send is pressed; a standing steer in force offers Withdraw.
 */
export function SteerCardView(props: {
  readonly draft: SteerDraft;
  readonly view: CardView;
  readonly actions: CardActions;
}): JSX.Element | null {
  const {draft, view, actions} = props;
  if (view.phase === 'dismissed') return null;
  const editing = view.phase === 'draft';
  return (
    <Card section="Steer">
      {editing ? (
        <Draft draft={draft} view={view} actions={actions} />
      ) : (
        <Stack gap="space-3">
          <Text>{draft.text}</Text>
          <Row gap="space-3" wrap>
            {view.status === null ? null : (
              <Chip label={view.status.label} tone={view.status.tone} dot />
            )}
            <Text variant="small" tone="muted">
              {view.summary}
            </Text>
          </Row>
          {view.error === null ? null : (
            <Text variant="small" tone="err">
              {view.error}
            </Text>
          )}
          {view.withdrawable ? (
            <Row gap="space-3">
              <Button variant="secondary" size="sm" text="Withdraw" press={actions.withdraw} />
            </Row>
          ) : null}
        </Stack>
      )}
    </Card>
  );
}
