import {
  Card,
  Chip,
  Grid,
  Page,
  Reserve,
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
import {
  APPEARANCE_MODES,
  type Appearance,
  THEMES,
  type ThemeMode,
  themesOf,
} from '@vibesys/ui/tokens';
import type {JSX, ReactNode} from 'react';
import {
  type AppearanceState,
  type ResourceRow,
  SETTINGS_TABS,
  type SettingsTab,
  themeShown,
  withMode,
  withTheme,
} from '../../state/index.js';
import {HostStatusChip} from '../host-status-chip.js';
import {MODE_WORD, TAB_WORD, themeWord} from './words.js';

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

function AppearanceTab(props: {
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

const COLUMNS: readonly TableColumn<ResourceRow>[] = [
  {key: 'name', header: 'Name', lead: true, clip: row => row.label, cell: row => row.label},
  {
    key: 'status',
    header: 'Status',
    cell: row => <HostStatusChip status={row.status} even />,
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
    <Page width="wide" state="ready" title="Settings">
      <SectionLayout
        label="Settings sections"
        sections={
          // The items sit straight in the navigation, which stacks them beside the content and lays
          // them in one row, as wide as each other, above it when the page is narrow.
          SETTINGS_TABS.map(tab => (
            <SidebarItem
              key={tab}
              label={TAB_WORD[tab]}
              active={tab === props.tab}
              press={() => props.pick(tab)}
            />
          ))
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
