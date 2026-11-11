import {Fact, Text} from '@vibesys/ui';
import type {JSX} from 'react';

/**
 * One fact of a facts line: its figure in the text color and its words muted, in one run of text so
 * the space between them is a real space (a fact is a flex box, which drops a space between its
 * children). With a `hint` the pointer says more.
 */
export function Counted(props: {
  readonly figure: string;
  readonly words: string;
  readonly hint?: string;
}): JSX.Element {
  return (
    <Fact {...(props.hint === undefined ? {} : {title: props.hint})}>
      <Text variant="small" tone="muted">
        <Text variant="small">{props.figure}</Text>
        {` ${props.words}`}
      </Text>
    </Fact>
  );
}

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
