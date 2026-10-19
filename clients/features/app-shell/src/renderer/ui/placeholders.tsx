import {Page, Text} from '@vibesys/ui';
import type {JSX} from 'react';

/** A screen not built yet: its title alone. */
export function PlaceholderScreen(props: {readonly title: string}): JSX.Element {
  return (
    <Page>
      <Text as="h1" variant="title">
        {props.title}
      </Text>
    </Page>
  );
}
