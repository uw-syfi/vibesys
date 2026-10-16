import {Button, Page, Row, Stack, Text, type ThemeName} from '@vibesys/ui';
import {THEMES} from '@vibesys/ui/tokens';
import type {JSX} from 'react';
import {type ComponentName, REGISTRY} from './registry.js';

const NAMES = Object.keys(REGISTRY) as ComponentName[];
const THEME_NAMES = Object.keys(THEMES) as ThemeName[];

/** The theme a `?theme=` query names, or null when it names none of the six. */
export function themeFromQuery(search: string): ThemeName | null {
  const name = new URLSearchParams(search).get('theme');
  return THEME_NAMES.find(theme => theme === name) ?? null;
}

function Entry(props: {readonly name: ComponentName}): JSX.Element {
  const headingId = `entry-${props.name}`;
  return (
    <section data-gallery-entry={props.name} aria-labelledby={headingId}>
      <Stack gap="space-4">
        <Text as="h2" variant="section" id={headingId}>
          {props.name}
        </Text>
        {REGISTRY[props.name].render()}
      </Stack>
    </section>
  );
}

/** Every public component in every state, in `theme`, with links to the other themes. */
export function Gallery(props: {readonly theme: ThemeName}): JSX.Element {
  return (
    <Page width="wide">
      <Stack gap="space-3">
        <Text as="h1" variant="title">
          Components in {props.theme}
        </Text>
        <nav aria-label="Themes">
          <Row gap="space-2" wrap>
            {THEME_NAMES.map(theme => (
              <Button
                key={theme}
                size="sm"
                variant={theme === props.theme ? 'primary' : 'quiet'}
                text={theme}
                href={`?theme=${theme}`}
              />
            ))}
          </Row>
        </nav>
      </Stack>
      {NAMES.map(name => (
        <Entry key={name} name={name} />
      ))}
    </Page>
  );
}
