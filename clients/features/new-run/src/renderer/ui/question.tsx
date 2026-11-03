/**
 * The frame every page of the guided form shares: the step's label, the question as the page's
 * title, the page's content, and the one action row that moves between steps.
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

/** The button that goes on: disabled, its reason only described, until the question is answered. */
export function NextButton(props: {
  readonly text: string;
  readonly press: () => void;
  /** Why the step cannot go on yet ('' when the unanswered question is the reason); absent when it can. */
  readonly blocked: string | undefined;
}): JSX.Element {
  return (
    <Button
      variant="primary"
      text={props.text}
      press={props.press}
      // The page says what is missing; the reason is the button's description, not a caption.
      {...(props.blocked === undefined ? {} : {disabledReason: props.blocked, quietReason: true})}
    />
  );
}

/**
 * The action row every step ends with, in one order on every step: Back, where it always is, with
 * Start over beside it; at the other end the screen's status and the way on, always the last button.
 */
export function Nav(props: {
  readonly startOver?: {readonly text: string; readonly press: () => void};
  /** What failed and its own way on, beside the button it holds back. */
  readonly status: ReactNode;
  readonly back?: () => void;
  readonly next: ReactNode;
}): JSX.Element {
  const {startOver} = props;
  return (
    // Top-aligned: a reason Start run gives hangs below the row, not inside its height.
    <Row justify="between" align="start" wrap>
      <Row gap="space-3" align="start">
        {props.back === undefined ? null : <Button text="Back" press={props.back} />}
        {startOver === undefined ? null : (
          <Button variant="quiet" text={startOver.text} press={startOver.press} />
        )}
      </Row>
      <Row gap="space-3" align="start" wrap>
        {props.status}
        {props.next}
      </Row>
    </Row>
  );
}
