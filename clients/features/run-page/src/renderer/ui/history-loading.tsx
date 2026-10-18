/**
 * What the Run screen shows in place of an ended run while its history is being read: the read's
 * progress (a bar and "n of N events" once the total is known, else the events and bytes so far).
 * The hero, panes and counts wait for the whole history, since a prefix of it would read as a run
 * still going.
 */
import {Card, Meter, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {type HistoryProgress, historyLoadingView} from '../../state/index.js';

export function HistoryLoading(props: {readonly progress: HistoryProgress}): JSX.Element {
  const view = historyLoadingView(props.progress);
  return (
    <Card>
      <Stack gap="space-3">
        <Text variant="heading">{view.title}</Text>
        {view.share === null ? null : (
          <Meter label="Share of the run’s events read" value={view.share} />
        )}
        <Text variant="code" tone="muted">
          {view.detail}
        </Text>
      </Stack>
    </Card>
  );
}
