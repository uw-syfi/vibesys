/**
 * The frame every page of the guided form shares: the step's label, the question as the page's
 * title, an optional line under it, the page's content, and the row that moves between steps.
 */
import {Button, Row, Stack, Text} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';

export function Question(props: {
  /** Where the form stands ("Step 3 of 8"), above the question. */
  readonly kicker: string;
  readonly title: string;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <Stack gap="space-5">
      <Stack gap="space-2">
        <Text variant="section">{props.kicker}</Text>
        <Text as="h1" variant="title">
          {props.title}
        </Text>
      </Stack>
      {props.children}
    </Stack>
  );
}

/** Back, and the button that goes on: disabled, with its reason, until the question is answered. */
export function Nav(props: {
  readonly back?: () => void;
  readonly next: {
    readonly text: string;
    readonly press: () => void;
    /** Why the step cannot go on yet; absent when it can. */
    readonly blocked?: string | undefined;
  };
}): JSX.Element {
  const {blocked} = props.next;
  return (
    // Top-aligned: the reason a blocked Continue gives hangs below the row, not inside its height.
    <Row justify="between" align="start" wrap>
      {props.back === undefined ? <span /> : <Button text="Back" press={props.back} />}
      <Button
        variant="primary"
        text={props.next.text}
        press={props.next.press}
        {...(blocked === undefined ? {} : {disabledReason: blocked, quietReason: blocked === ''})}
      />
    </Row>
  );
}
