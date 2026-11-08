import {Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {TimelineRow} from '../../../state/index.js';
import {useDisplayClock} from '../../display-clock.js';
import {changeWord} from './settings-words.js';

/**
 * How many lines the changes show. The list is always this many lines high, filled from the top, so a
 * change added while the sheet is open takes a line that was already there and moves nothing; the
 * heading says how many there are when more than fit.
 */
const LINES = 5;

/** A line with nothing in it, as high as a line of text. */
const BLANK = ' ';

/**
 * The settings changes of a run, newest first: one line for each setting that changed, with when, as
 * labels and numbers. A run nobody changed has none.
 */
export function SettingsTimeline(props: {readonly rows: readonly TimelineRow[]}): JSX.Element {
  const clock = useDisplayClock();
  const lines = props.rows.flatMap(row =>
    row.changes.map(change => ({
      key: `${row.sequence}-${change.kind}`,
      at: clock.reportMoment(row.atMs),
      words: changeWord(change),
    })),
  );
  const slots = Array.from({length: LINES}, (_, index) => {
    const line = lines[index];
    return line === undefined
      ? {
          key: `blank-${index}`,
          at: '',
          words: index === 0 && lines.length === 0 ? 'None' : BLANK,
          blank: true,
        }
      : {...line, blank: false};
  });
  return (
    <Stack gap="space-2">
      <Text variant="section">
        {lines.length > LINES ? `Changes · ${lines.length}` : 'Changes'}
      </Text>
      <Stack gap="space-1">
        {slots.map(slot => (
          <Row key={slot.key} gap="space-3" align="baseline">
            <Text variant="small" tone="muted">
              {slot.at}
            </Text>
            <Text variant="small" {...(slot.blank ? {tone: 'muted' as const} : {})}>
              {slot.words}
            </Text>
          </Row>
        ))}
      </Stack>
    </Stack>
  );
}
