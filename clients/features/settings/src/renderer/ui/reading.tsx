/**
 * What a machine section shows while its reading is awaited, failed, or answered by a helper that
 * predates it; once answered, the section's own content.
 */
import {Banner, Button, Card, EmptyState} from '@vibesys/ui';
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
    return (
      <Banner
        tone="err"
        title={`Could not read ${props.what} on ${machine}.`}
        {...failureWords(reading)}
        actions={<Button text="Try again" press={props.retry} />}
      />
    );
  }
  if (reading.offered.kind === 'unsupported') {
    return (
      <Banner
        tone="warn"
        title={`The VibeSys on ${machine} is too old for this.`}
        body="Update its checkout in Machines, then come back."
      />
    );
  }
  return <>{props.children(reading.offered.value)}</>;
}
