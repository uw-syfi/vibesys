import {
  Banner,
  Button,
  Card,
  EmptyState,
  ErrorActions,
  Field,
  Grid,
  Page,
  Row,
  SectionLayout,
  SegmentedControl,
  Select,
  SidebarItem,
  SidebarSection,
  Stack,
  Switch,
  Text,
  ThemeSwatch,
} from '@vibesys/ui';
import {type Appearance, type AppearanceMode, type ThemeName, themesOf} from '@vibesys/ui/tokens';
import type {JSX, ReactNode} from 'react';
import {
  type MachineSettingsAction,
  type MachineSettingsState,
  machinesDetail,
  type SettingsAction,
  type SettingsSection,
  type SettingsState,
  shownMachine,
} from '../../state/index.js';
import {ClustersSection} from './clusters-section.js';
import {LoginsSection} from './logins-section.js';

const MODES: readonly {readonly value: AppearanceMode; readonly label: string}[] = [
  {value: 'system', label: 'Match system'},
  {value: 'light', label: 'Always light'},
  {value: 'dark', label: 'Always dark'},
];

const SECTION_NAMES: Readonly<Record<SettingsSection, string>> = {
  clusters: 'Clusters and clouds',
  logins: 'Agent logins',
  machines: 'Machines',
  appearance: 'Appearance',
  notifications: 'Notifications',
};

function themeLabel(theme: ThemeName): string {
  return `${theme.charAt(0).toUpperCase()}${theme.slice(1)}`;
}

/** The theme in use, with the system's mode when it picks one: `Light · Paper`. */
function modeNote(appearance: Appearance, os: 'light' | 'dark'): string {
  const mode = appearance.mode === 'system' ? os : appearance.mode;
  return `${mode === 'light' ? 'Light' : 'Dark'} · ${themeLabel(appearance[mode])}`;
}

function ThemeChoice(props: {
  readonly mode: 'light' | 'dark';
  readonly appearance: Appearance;
  readonly choose: (appearance: Appearance) => void;
}): JSX.Element {
  const {mode, appearance, choose} = props;
  return (
    <Card section={mode === 'light' ? 'Light theme' : 'Dark theme'}>
      <Grid min="swatch-row" gap="space-3">
        {themesOf(mode).map(theme => (
          <ThemeSwatch
            key={theme}
            theme={theme}
            label={themeLabel(theme)}
            selected={appearance[mode] === theme}
            press={() => choose({...appearance, [mode]: theme})}
          />
        ))}
      </Grid>
    </Card>
  );
}

interface MachineSettingsProps {
  readonly state: MachineSettingsState;
  readonly dispatch: (action: MachineSettingsAction) => void;
  readonly machines: readonly {readonly host: string; readonly label: string}[];
}

interface MachineCount {
  readonly count: number;
  readonly attention: number;
  readonly listed: boolean;
}

/** Clusters and clouds, or Agent logins, of the machine the selector shows. */
function MachineContent(props: {
  readonly which: 'clusters' | 'logins';
  readonly machineSettings: MachineSettingsProps;
}): JSX.Element {
  const {which, machineSettings} = props;
  const host = shownMachine(machineSettings.state);
  const machine = machineSettings.machines.find(each => each.host === host);
  if (host === null || machine === undefined) {
    return (
      <Card>
        <EmptyState
          title="No machine yet"
          action={
            <Button
              text="Add a machine"
              press={() => machineSettings.dispatch({type: 'section', section: 'machines'})}
            />
          }
        />
      </Card>
    );
  }
  const Section = which === 'clusters' ? ClustersSection : LoginsSection;
  return (
    <Section
      host={host}
      machine={machine.label}
      view={machineSettings.state.views[host]}
      dispatch={machineSettings.dispatch}
    />
  );
}

/** Notifications or Appearance, from the stored preferences. */
function AppContent(props: {
  readonly section: 'appearance' | 'notifications';
  readonly state: SettingsState;
  readonly dispatch: (action: SettingsAction) => void;
}): JSX.Element {
  const {section, state, dispatch} = props;
  if (state.kind === 'loading') return <Text variant="body">Loading settings…</Text>;
  const {appearance, notifications} = state.shown;
  const choose = (next: Appearance): void =>
    dispatch({type: 'change', change: {kind: 'appearance', appearance: next}});
  if (section === 'notifications') {
    return (
      <Stack>
        <Text as="h1" variant="title">
          Notifications
        </Text>
        {state.problem === null ? null : (
          <Banner
            tone="err"
            title="Could not save the change."
            body={state.problem}
            actions={<ErrorActions copy={`Could not save the change.\n\n${state.problem}`} />}
          />
        )}
        <Card section="Run notifications">
          <Switch
            label="Notify me when a run finishes, fails or pauses"
            checked={notifications.enabled}
            change={enabled => dispatch({type: 'change', change: {kind: 'notifications', enabled}})}
          />
        </Card>
      </Stack>
    );
  }
  return (
    <Stack>
      <Text as="h1" variant="title">
        Appearance
      </Text>
      {state.problem === null ? null : (
        <Banner
          tone="err"
          title="Could not save the change."
          body={state.problem}
          actions={<ErrorActions copy={`Could not save the change.\n\n${state.problem}`} />}
        />
      )}
      <Card section="Mode">
        <SegmentedControl
          label="Appearance mode"
          options={MODES}
          value={appearance.mode}
          change={mode => choose({...appearance, mode})}
        />
        <Text variant="small" tone="muted">
          {modeNote(appearance, state.os)}
        </Text>
      </Card>
      <ThemeChoice mode="light" appearance={appearance} choose={choose} />
      <ThemeChoice mode="dark" appearance={appearance} choose={choose} />
    </Stack>
  );
}

/** The machine selector and what the machine has, then the app's own sections. */
function SettingsNav(props: {
  readonly machineSettings: MachineSettingsProps;
  readonly machines: MachineCount;
}): JSX.Element {
  const {machineSettings} = props;
  const {section} = machineSettings.state;
  const shown = machineSettings.machines.find(
    each => each.host === shownMachine(machineSettings.state),
  );
  const go = (next: SettingsSection) => () =>
    machineSettings.dispatch({type: 'section', section: next});
  return (
    <>
      {shown === undefined ? null : (
        <Field label="Machine">
          <Select
            value={shown.host}
            options={machineSettings.machines.map(each => ({value: each.host, label: each.label}))}
            change={host => machineSettings.dispatch({type: 'machine', host})}
          />
        </Field>
      )}
      <SidebarItem
        label="Clusters and clouds"
        active={section === 'clusters'}
        press={go('clusters')}
      />
      <SidebarItem label="Agent logins" active={section === 'logins'} press={go('logins')} />
      <SidebarSection title="This app">
        <SidebarItem
          label="Appearance"
          active={section === 'appearance'}
          press={go('appearance')}
        />
        <SidebarItem
          label="Machines"
          {...(props.machines.listed
            ? {
                detail: machinesDetail(props.machines.count, props.machines.attention),
              }
            : {})}
          active={section === 'machines'}
          press={go('machines')}
        />
        <SidebarItem
          label="Notifications"
          active={section === 'notifications'}
          press={go('notifications')}
        />
      </SidebarSection>
    </>
  );
}

/**
 * Settings sections beside their content: a machine selector and, under it, what the machine has
 * (Clusters and clouds, Agent logins), then the app's own (Appearance, Machines, Notifications),
 * with Machines supplied by the shell that owns it.
 */
export function SettingsScreen(props: {
  readonly state: SettingsState;
  readonly dispatch: (action: SettingsAction) => void;
  readonly back: () => void;
  readonly machinesContent: ReactNode;
  /** The machine sections' store, and the machines they can be about. */
  readonly machineSettings: MachineSettingsProps;
  /** The Machines section's own count; while it is not `listed`, no count is worded. */
  readonly machines: MachineCount;
}): JSX.Element {
  const {section} = props.machineSettings.state;
  const shown = props.machineSettings.machines.find(
    each => each.host === shownMachine(props.machineSettings.state),
  );
  const summary =
    section === 'clusters' || section === 'logins'
      ? `${shown?.label ?? 'No machine'} · ${SECTION_NAMES[section]}`
      : `This app · ${SECTION_NAMES[section]}`;
  return (
    <Page
      width="wide"
      header={
        <Row gap="space-2">
          <Button variant="quiet" size="sm" icon="chevron-left" label="Back" press={props.back} />
          <Text variant="section">Settings</Text>
        </Row>
      }
    >
      <SectionLayout
        label="Settings sections"
        summary={summary}
        sections={<SettingsNav machineSettings={props.machineSettings} machines={props.machines} />}
      >
        {section === 'machines' ? (
          props.machinesContent
        ) : section === 'clusters' || section === 'logins' ? (
          <MachineContent which={section} machineSettings={props.machineSettings} />
        ) : (
          <AppContent section={section} state={props.state} dispatch={props.dispatch} />
        )}
      </SectionLayout>
    </Page>
  );
}
