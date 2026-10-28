/**
 * How far the run's history has loaded, in a bar and two numbers; the failure of that load as a
 * status word with Retry. The screen paints from the keyframe while the history follows.
 */
import {Button, Card, ErrorActions, LoadingBar, Meter, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {LoadView} from '../../state/index.js';

export function LoadStatus(props: {
  readonly view: LoadView;
  readonly retry: () => void;
}): JSX.Element | null {
  const {view} = props;
  switch (view.kind) {
    case 'ready':
      return null;
    case 'loading':
      return (
        <Stack gap="space-1">
          {view.label === 'Skeleton' ? (
            <LoadingBar label={view.label} />
          ) : (
            <Meter label={view.label} value={view.fraction} />
          )}
          <Text variant="code" tone="muted">
            {[view.label, view.detail].filter(word => word !== '').join(' ')}
          </Text>
        </Stack>
      );
    case 'reconnecting':
      return (
        <Stack gap="space-1">
          <LoadingBar label="Reconnecting" />
          <Text variant="code" tone="muted">
            Reconnecting
          </Text>
        </Stack>
      );
    case 'failed':
      return (
        <Card>
          <Stack gap="space-2" align="start">
            <Text variant="heading">{view.word}</Text>
            <Row gap="space-2" wrap>
              <Button variant="secondary" text="Retry" press={props.retry} />
              <ErrorActions copy={[view.word, view.detail].filter(Boolean).join('\n\n')} />
            </Row>
          </Stack>
        </Card>
      );
  }
}
