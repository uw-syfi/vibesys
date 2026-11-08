import {Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {TimelineRow} from '../../../state/index.js';
import {useDisplayClock} from '../../display-clock.js';
import {changeWord} from './settings-words.js';

/**
 * The settings changes of a run, newest first: when, then each setting that changed, as labels and
 * numbers. A run nobody changed has none.
 */
export function SettingsTimeline(props: {readonly rows: readonly TimelineRow[]}): JSX.Element {
  const clock = useDisplayClock();
  return (
    <Stack gap="space-2">
      <Text variant="section">Changes</Text>
      {props.rows.length === 0 ? (
        <Text variant="small" tone="muted">
          None
        </Text>
      ) : (
        props.rows.map(row => (
          <Row key={String(row.sequence)} gap="space-3" align="baseline" wrap>
            <Text variant="small" tone="muted">
              {clock.reportMoment(row.atMs)}
            </Text>
            <Stack gap="space-1">
              {row.changes.map(change => (
                <Text key={change.kind} variant="small">
                  {changeWord(change)}
                </Text>
              ))}
            </Stack>
          </Row>
        ))
      )}
    </Stack>
  );
}
