/** The frame both pages share: the step's label, the question as the page's title, and the page. */
import {Stack, Text} from '@vibesys/ui';
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
