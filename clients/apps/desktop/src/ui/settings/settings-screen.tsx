import {
  Button,
  Card,
  Chip,
  Grid,
  KeyValue,
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
import {
  type AppearanceState,
  themeShown,
  type Viewer,
  withMode,
  withTheme,
} from '../../state/index.js';
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
    <Card section="Appearance">
      <Stack gap="space-6">
        <Text tone="muted">
          Follow the operating system or keep a light or dark workspace. Theme choices are saved for
          this desktop app.
        </Text>
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

/** Machine actions already owned by the shell; Settings only links to their destination. */
export interface SettingsMachineControls {
  /** Machines represented in this window now, including this machine when it is available. */
  readonly count: number;
  readonly manage: () => void;
  /** Absent in a read-only window that cannot change the saved machine roster. */
  readonly add?: () => void;
}

/** Build facts safe to display as diagnostics. */
export interface SettingsDiagnostics {
  readonly protocolVersion: string;
}

function AccountsCard(props: {
  readonly viewer: Viewer | null;
  readonly machines: SettingsMachineControls | undefined;
}): JSX.Element {
  const identity =
    props.viewer === null
      ? [
          {key: 'Operator label', value: 'Unavailable'},
          {key: 'Install identity', value: 'Unavailable'},
        ]
      : [
          {key: 'Operator label', value: props.viewer.label},
          {key: 'Install identity', value: props.viewer.id, mono: true},
        ];
  return (
    <Card section="Accounts & providers">
      <Stack gap="space-4">
        <Text tone="muted">
          This identity labels your check-ins for Catch me up. It is not a login and grants no
          access to a machine or provider.
        </Text>
        <KeyValue items={identity} />
        <Stack gap="space-2" apart>
          <Text variant="section">CLI providers</Text>
          <Text tone="muted">
            Provider CLIs and their credentials stay on the machine that runs the work. VibeSys does
            not sign in to provider accounts from this page.
          </Text>
          {props.machines === undefined ? null : (
            <Row>
              <Button
                variant="secondary"
                icon="monitor"
                text="Review provider machines"
                press={props.machines.manage}
              />
            </Row>
          )}
        </Stack>
      </Stack>
    </Card>
  );
}

function RunsCard(): JSX.Element {
  return (
    <Card section="Runs">
      <Stack gap="space-5">
        <Text tone="muted">
          Run-specific cost, time, concurrency and lead settings stay with each run, where their
          current values and change history are available together.
        </Text>
        <Grid min="tile">
          <Stack gap="space-2">
            <Row gap="space-3" wrap>
              <Text variant="section">Desktop notifications</Text>
              <Chip label="Not available" tone="neutral" />
            </Row>
            <Text tone="muted">
              This build does not deliver desktop run notifications, so Settings does not show a
              switch that would have no effect.
            </Text>
          </Stack>
          <Stack gap="space-2">
            <Row gap="space-3" wrap>
              <Text variant="section">Run archive</Text>
              <Chip label="Not available" tone="neutral" />
            </Row>
            <Text tone="muted">
              Runs cannot be archived or restored here yet. Ended runs remain available from the
              Runs page and its Ended filter.
            </Text>
          </Stack>
        </Grid>
      </Stack>
    </Card>
  );
}

function MachinesCard(props: {readonly controls: SettingsMachineControls}): JSX.Element {
  const {controls} = props;
  return (
    <Card
      section="Machines & servers"
      action={
        <Chip
          label={`${controls.count} ${controls.count === 1 ? 'machine' : 'machines'} available`}
          tone="neutral"
        />
      }
    >
      <Stack gap="space-4">
        <Text tone="muted">
          Inspect connection health and resources on the Machines page, or add an SSH machine or
          server through its dedicated connection form.
        </Text>
        <Row gap="space-3" wrap>
          <Button
            variant="secondary"
            icon="monitor"
            text="Manage machines"
            press={controls.manage}
          />
          {controls.add === undefined ? null : (
            <Button
              variant="secondary"
              icon="plus"
              text="Add machine or server"
              press={controls.add}
            />
          )}
        </Row>
      </Stack>
    </Card>
  );
}

function AboutCard(props: {
  readonly diagnostics: SettingsDiagnostics;
  readonly machineCount: number | null;
}): JSX.Element {
  return (
    <Card section="About & diagnostics">
      <Stack gap="space-4">
        <Text tone="muted">
          VibeSys Desktop coordinates runs on local and remote machines. These build facts help
          diagnose compatibility without exposing provider credentials.
        </Text>
        <KeyValue
          items={[
            {key: 'Client', value: 'VibeSys Desktop'},
            {key: 'Host protocol', value: props.diagnostics.protocolVersion, mono: true},
            {
              key: 'Machines in this window',
              value: props.machineCount === null ? 'Unavailable' : String(props.machineCount),
            },
          ]}
        />
      </Stack>
    </Card>
  );
}

/** Settings: appearance, real install identity, run capabilities, machines and diagnostics. */
export function SettingsScreen(props: {
  readonly appearance: AppearanceState;
  readonly choose: (appearance: Appearance) => void;
  /** This install as hosts see it; null when the main process could not provide it. */
  readonly viewer?: Viewer | null;
  /** Navigation to the existing Machines destination and its Add flow. */
  readonly machines?: SettingsMachineControls;
  /** Facts derived from this client build, never guessed from a host. */
  readonly diagnostics?: SettingsDiagnostics;
  /** The Repositories folders of the machines; a window that cannot keep them shows the appearance only. */
  readonly repositories?: RepositoriesControls;
}): JSX.Element {
  return (
    <Page
      width="wide"
      state="ready"
      title="Settings"
      meta={
        <Text tone="muted">
          Desktop preferences, operator identity, run capabilities and machine defaults
        </Text>
      }
    >
      <Stack gap="space-4">
        <AppearanceCard state={props.appearance} choose={props.choose} />
        <Grid min="tile">
          <AccountsCard viewer={props.viewer ?? null} machines={props.machines} />
          <RunsCard />
        </Grid>
        {props.machines === undefined ? null : <MachinesCard controls={props.machines} />}
        {props.repositories === undefined ? null : (
          <RepositoriesCard controls={props.repositories} />
        )}
        {props.diagnostics === undefined ? null : (
          <AboutCard diagnostics={props.diagnostics} machineCount={props.machines?.count ?? null} />
        )}
      </Stack>
    </Page>
  );
}
