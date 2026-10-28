/**
 * A running idea's activity as one status line: a live dot (amber once the idea has gone quiet),
 * what it is doing, and how long since its news. The open idea's pane leads with it, and its Logs
 * pane shows it while there is no log yet.
 */
import {Card, Row, StatusDot, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {activityAge, activityWords, type IdeaActivity} from '../../state/index.js';

export function ActivityStatus(props: {
  readonly activity: IdeaActivity;
  /** The run's newest news (`activityNow`); null when the run clock is unknown. */
  readonly now: number | null;
}): JSX.Element {
  const age = activityAge(props.activity, props.now);
  const quiet = age?.quiet === true;
  return (
    <Card>
      <Row gap="space-2">
        <StatusDot tone={quiet ? 'warn' : 'live'} label={quiet ? 'Quiet' : 'Working'} />
        <Row grow>
          <Text>{activityWords(props.activity)}</Text>
        </Row>
        {age === null ? null : (
          <Text variant="small" tone={quiet ? 'warn' : 'muted'}>
            {age.text}
          </Text>
        )}
      </Row>
    </Card>
  );
}
