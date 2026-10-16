import {Card, EmptyState, Page, Stack, Text, Wordmark} from '@vibesys/ui';
import type {JSX} from 'react';
import type {Screen} from '../../state/index.js';

/** The app's ASCII wordmark, the New run hero. */
export const WORDMARK = [
  '██╗   ██╗██╗██████╗ ███████╗███████╗██╗   ██╗███████╗',
  '██║   ██║██║██╔══██╗██╔════╝██╔════╝╚██╗ ██╔╝██╔════╝',
  '██║   ██║██║██████╔╝█████╗  ███████╗ ╚████╔╝ ███████╗',
  '╚██╗ ██╔╝██║██╔══██╗██╔══╝  ╚════██║  ╚██╔╝  ╚════██║',
  ' ╚████╔╝ ██║██████╔╝███████╗███████║   ██║   ███████║',
  '  ╚═══╝  ╚═╝╚═════╝ ╚══════╝╚══════╝   ╚═╝   ╚══════╝',
].join('\n');

const COMING: Readonly<Record<Exclude<Screen['kind'], 'settings'>, {title: string; body: string}>> =
  {
    'new-run': {
      title: 'New run is on its way',
      body: 'Machine, project and task, agent and budget, with a ready-to-start check, arrive here next.',
    },
    machines: {
      title: 'Machines are on their way',
      body: 'Your SSH hosts and their VibeSys checkouts will be listed and checked here.',
    },
    'all-runs': {
      title: 'All runs are on their way',
      body: 'Every run of every project on your machines will be listed here.',
    },
    run: {
      title: 'The run screen is on its way',
      body: 'Its timeline, map, work and logs arrive here next.',
    },
  };

/** A screen not built yet: the New run hero with the wordmark, or a titled empty state. */
export function PlaceholderScreen(props: {
  readonly screen: Exclude<Screen, {readonly kind: 'settings'}>;
}): JSX.Element {
  const {title, body} = COMING[props.screen.kind];
  if (props.screen.kind !== 'new-run') {
    return (
      <Page>
        <Card>
          <EmptyState title={title} body={body} />
        </Card>
      </Page>
    );
  }
  return (
    <Page>
      <Card>
        <Stack gap="space-4">
          <Wordmark art={WORDMARK} label="VibeSys" align="center" tone="accent" />
          <Text as="h1" variant="title">
            New run
          </Text>
        </Stack>
      </Card>
      <Card>
        <EmptyState title={title} body={body} />
      </Card>
    </Page>
  );
}
