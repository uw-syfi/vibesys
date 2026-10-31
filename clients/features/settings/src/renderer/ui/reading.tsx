/**
 * What a machine section shows while its reading is awaited, failed, or answered by a helper that
 * predates it; once answered, the section's own content.
 */
import {Button, Card, Chip, EmptyState, Row} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {failureWords, type Reading as State} from '../../state/index.js';

export function Reading<T>(props: {
  readonly what: string;
  readonly machine: string;
  readonly reading: State<T> | undefined;
  readonly retry: () => void;
  readonly children: (value: T) => ReactNode;
}): JSX.Element {
  const {reading, machine} = props;
  if (reading === undefined || reading.kind === 'loading') {
    return (
      <Card>
        <EmptyState title={`Reading ${props.what} on ${machine}`} />
      </Card>
    );
  }
  if (reading.kind === 'failed') {
    const {body, hint} = failureWords(reading);
    return (
      <Row gap="space-3" align="center" wrap>
        <Chip
          label="Not read"
          tone="err"
          dot
          hint={[`Could not read ${props.what} on ${machine}.`, body, hint]
            .filter(part => part !== undefined)
            .join('\n')}
        />
        <Button text="Try again" press={props.retry} />
      </Row>
    );
  }
  if (reading.offered.kind === 'unsupported') {
    return (
      <Chip
        label="Needs an update"
        tone="warn"
        dot
        hint={`The VibeSys on ${machine} is too old for this. Update its checkout in Machines, then come back.`}
      />
    );
  }
  return <>{props.children(reading.offered.value)}</>;
}
