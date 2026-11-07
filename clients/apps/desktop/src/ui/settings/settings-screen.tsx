import {
  Card,
  Chip,
  Grid,
  Page,
  Row,
  SectionLayout,
  SegmentedControl,
  SidebarItem,
  Stack,
  Table,
  type TableColumn,
  Text,
  ThemeSwatch,
} from '@vibesys/ui';
import {APPEARANCE_MODES, type Appearance, type ThemeMode, themesOf} from '@vibesys/ui/tokens';
import type {JSX} from 'react';
import {
  type AppearanceState,
  type ResourceRow,
  SETTINGS_TABS,
  type SettingsTab,
  withMode,
  withTheme,
} from '../../state/index.js';
import {CONNECTION_WORD, FRESHNESS_WORD, MODE_WORD, TAB_WORD, themeWord} from './words.js';

const MODES = APPEARANCE_MODES.map(value => ({value, label: MODE_WORD[value]}));
const THEME_MODES: readonly {readonly mode: ThemeMode; readonly section: string}[] = [
  {mode: 'light', section: 'Light theme'},
  {mode: 'dark', section: 'Dark theme'},
];

function AppearanceTab(props: {
  readonly state: AppearanceState;
  readonly choose: (appearance: Appearance) => void;
}): JSX.Element {
  const {appearance, unsaved} = props.state;
  return (
    <Stack gap="space-5">
      <Card section="Mode">
        <Row gap="space-4" wrap>
          <SegmentedControl
            label="Mode"
            options={MODES}
            value={appearance.mode}
            change={mode => props.choose(withMode(appearance, mode))}
          />
          {unsaved ? <Chip label="Not saved" tone="warn" hint="The last save failed." /> : null}
        </Row>
      </Card>
      {THEME_MODES.map(({mode, section}) => (
        <Card key={mode} section={section}>
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
        </Card>
      ))}
    </Stack>
  );
}

const COLUMNS: readonly TableColumn<ResourceRow>[] = [
  {key: 'name', header: 'Name', lead: true, clip: row => row.label, cell: row => row.label},
  {
    key: 'state',
    header: 'State',
    cell: row => {
      const word = CONNECTION_WORD[row.connection];
      return <Chip label={word.label} tone={word.tone} dot />;
    },
  },
  {
    key: 'data',
    header: 'Data',
    cell: row => {
      const word = FRESHNESS_WORD[row.freshness];
      return <Chip label={word.label} tone={word.tone} dot />;
    },
  },
];

function ResourcesTab(props: {readonly resources: readonly ResourceRow[]}): JSX.Element {
  return (
    <Table
      label="Resources"
      columns={COLUMNS}
      rows={props.resources}
      rowKey={row => row.key}
      middle
    />
  );
}

/** Settings: a list of tabs down the left and the chosen tab's content beside it. */
export function SettingsScreen(props: {
  readonly tab: SettingsTab;
  readonly pick: (tab: SettingsTab) => void;
  readonly appearance: AppearanceState;
  readonly choose: (appearance: Appearance) => void;
  readonly resources: readonly ResourceRow[];
}): JSX.Element {
  return (
    <Page
      width="wide"
      state="ready"
      header={
        <Text variant="title" as="h1">
          Settings
        </Text>
      }
    >
      <SectionLayout
        label="Settings sections"
        sections={
          <Stack gap="space-1">
            {SETTINGS_TABS.map(tab => (
              <SidebarItem
                key={tab}
                label={TAB_WORD[tab]}
                active={tab === props.tab}
                press={() => props.pick(tab)}
              />
            ))}
          </Stack>
        }
      >
        {props.tab === 'appearance' ? (
          <AppearanceTab state={props.appearance} choose={props.choose} />
        ) : (
          <ResourcesTab resources={props.resources} />
        )}
      </SectionLayout>
    </Page>
  );
}
