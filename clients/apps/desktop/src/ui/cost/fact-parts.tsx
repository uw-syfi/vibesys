import {Fact, Text} from '@vibesys/ui';
import type {JSX} from 'react';

/** A count in the facts line's emphasis, with what it counts after it: `9 turns`. */
export function Counted(props: {readonly count: string; readonly what: string}): JSX.Element {
  return (
    <Fact>
      <Text variant="body-strong">{props.count}</Text>{' '}
      <Text variant="small" tone="muted">
        {props.what}
      </Text>
    </Fact>
  );
}

/** The dot between the groups of facts on one line. */
export const FactDot = (): JSX.Element => (
  <Fact>
    <Text variant="small" tone="muted">
      ·
    </Text>
  </Fact>
);

/** A fact in the quiet voice, with what it means on hover. */
export function MutedFact(props: {readonly text: string; readonly hint?: string}): JSX.Element {
  return (
    <Fact {...(props.hint === undefined ? {} : {title: props.hint})}>
      <Text variant="small" tone="muted">
        {props.text}
      </Text>
    </Fact>
  );
}
