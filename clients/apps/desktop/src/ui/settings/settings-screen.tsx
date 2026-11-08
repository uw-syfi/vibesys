import {
  Card,
  Chip,
  Grid,
  Page,
  Reserve,
  Row,
  SegmentedControl,
  Stack,
  Text,
  ThemeSwatch,
} from '@vibesys/ui';
import {
  APPEARANCE_MODES,
  type Appearance,
  THEMES,
  type ThemeMode,
  themesOf,
} from '@vibesys/ui/tokens';
import type {JSX, ReactNode} from 'react';
import {type AppearanceState, themeShown, withMode, withTheme} from '../../state/index.js';
import {RepositoriesCard, type RepositoriesControls} from './repositories-card.js';
import {MODE_WORD, themeWord} from './words.js';

const MODES = APPEARANCE_MODES.map(value => ({value, label: MODE_WORD[value]}));
const THEME_MODES: readonly {readonly mode: ThemeMode; readonly section: string}[] = [
  {mode: 'light', section: 'Light theme'},
  {mode: 'dark', section: 'Dark theme'},
];

/**
 * A group of the appearance: its label, with a status chip beside it, over its controls. The chip's
 * room is kept while it is not shown, so the controls below never move when it comes and goes.
 */
function Group(props: {
  readonly label: string;
  readonly chip: ReactNode;
  readonly shown: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <Stack gap="space-3">
      <Row gap="space-3">
        <Text variant="section">{props.label}</Text>
        {props.shown ? props.chip : <Reserve>{props.chip}</Reserve>}
      </Row>
      {props.children}
    </Stack>
  );
}

function AppearanceCard(props: {
  readonly state: AppearanceState;
  readonly choose: (appearance: Appearance) => void;
}): JSX.Element {
  const {appearance, unsaved} = props.state;
  const inUse = THEMES[themeShown(props.state)].mode;
  return (
    <Card>
      <Stack gap="space-6">
        <Group
          label="Mode"
          shown={unsaved}
          chip={<Chip label="Not saved" tone="warn" hint="The last save failed." />}
        >
          <Row gap="space-4" wrap>
            <SegmentedControl
              label="Mode"
              options={MODES}
              value={appearance.mode}
              change={mode => props.choose(withMode(appearance, mode))}
            />
          </Row>
        </Group>
        {THEME_MODES.map(({mode, section}) => (
          <Group
            key={mode}
            label={section}
            shown={mode === inUse}
            chip={<Chip label="In use" tone="neutral" />}
          >
            <Grid min="swatch-row">
              {themesOf(mode).map(theme => (
                <ThemeSwatch
                  key={theme}
                  theme={theme}
                  label={themeWord(theme)}
                  selected={appearance[mode] === theme}
                  press={() => props.choose(withTheme(appearance, theme))}
                />
              ))}
            </Grid>
          </Group>
        ))}
      </Stack>
    </Card>
  );
}

/** Settings: how the app looks, and where each machine's new repositories go. */
export function SettingsScreen(props: {
  readonly appearance: AppearanceState;
  readonly choose: (appearance: Appearance) => void;
  /** The Repositories folders of the machines; a window that cannot keep them shows the appearance only. */
  readonly repositories?: RepositoriesControls;
}): JSX.Element {
  return (
    <Page width="wide" state="ready" title="Settings">
      <Stack gap="space-4">
        <AppearanceCard state={props.appearance} choose={props.choose} />
        {props.repositories === undefined ? null : (
          <RepositoriesCard controls={props.repositories} />
        )}
      </Stack>
    </Page>
  );
}
