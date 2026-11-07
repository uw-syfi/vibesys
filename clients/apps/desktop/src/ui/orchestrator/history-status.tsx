import {Button, Chip, Row} from '@vibesys/ui';
import type {JSX} from 'react';
import type {RunHistoryStatus} from '../../core/index.js';

/**
 * Where the read of the open run's history stands, as a word beside the title: nothing once it is read,
 * and a way to read it again when it failed.
 */
export function HistoryStatusWord(props: {
  readonly status: RunHistoryStatus;
  readonly retry: () => void;
}): JSX.Element | null {
  const {status} = props;
  switch (status._tag) {
    case 'Idle':
      return null;
    case 'Loading':
      return <Chip label="Reading history" tone="live" dot />;
    case 'Failed':
      return (
        <Row gap="space-2">
          <Chip label="History not read" tone="warn" dot />
          <Button
            text="Retry"
            icon="refresh"
            variant="quiet"
            size="sm"
            hint="Read the run's history again"
            press={props.retry}
          />
        </Row>
      );
    default:
      status satisfies never;
      return null;
  }
}
