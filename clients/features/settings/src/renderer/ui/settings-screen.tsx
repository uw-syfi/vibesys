import {
  Banner,
  Button,
  Card,
  Grid,
  Page,
  Row,
  SegmentedControl,
  Stack,
  Switch,
  Text,
  ThemeSwatch,
} from '@vibesys/ui';
import {type Appearance, type AppearanceMode, type ThemeName, themesOf} from '@vibesys/ui/tokens';
import type {JSX} from 'react';
import type {SettingsAction, SettingsState} from '../../state/index.js';

const MODES: readonly {readonly value: AppearanceMode; readonly label: string}[] = [
  {value: 'system', label: 'Match system'},
  {value: 'light', label: 'Always light'},
  {value: 'dark', label: 'Always dark'},
];

function themeLabel(theme: ThemeName): string {
  return `${theme.charAt(0).toUpperCase()}${theme.slice(1)}`;
}

function modeNote(appearance: Appearance, os: 'light' | 'dark'): string {
  if (appearance.mode !== 'system') return `Always ${themeLabel(appearance[appearance.mode])}.`;
  return `Follows your system, which is ${os} now: ${themeLabel(appearance[os])}.`;
}

function ThemeChoice(props: {
  readonly mode: 'light' | 'dark';
  readonly appearance: Appearance;
  readonly choose: (appearance: Appearance) => void;
}): JSX.Element {
  const {mode, appearance, choose} = props;
  return (
    <Card section={mode === 'light' ? 'Light theme' : 'Dark theme'}>
      <Grid min="swatch" gap="space-3">
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

/** Settings: the appearance (mode, light and dark theme) and the run notifications. */
export function SettingsScreen(props: {
  readonly state: SettingsState;
  readonly dispatch: (action: SettingsAction) => void;
  readonly back: () => void;
  readonly openMachines: () => void;
  readonly machines: {readonly count: number; readonly attention: number};
}): JSX.Element {
  const {state, dispatch} = props;
  if (state.kind === 'loading') {
    return (
      <Page width="narrow">
        <Button variant="quiet" size="sm" icon="chevron-left" label="Back" press={props.back} />
        <Text variant="section">Settings</Text>
      </Page>
    );
  }
  const {appearance, notifications} = state.shown;
  const choose = (next: Appearance): void =>
    dispatch({type: 'change', change: {kind: 'appearance', appearance: next}});
  return (
    <Page
      width="narrow"
      header={
        <Stack gap="space-1">
          <Row gap="space-2">
            <Button variant="quiet" size="sm" icon="chevron-left" label="Back" press={props.back} />
            <Text variant="section">Settings</Text>
          </Row>
          <Text as="h1" variant="title">
            Appearance
          </Text>
        </Stack>
      }
    >
      <Card section="Machines">
        <Row justify="between" align="center">
          <Text variant="body">
            {props.machines.count} saved
            {props.machines.attention === 0 ? '' : ` · ${props.machines.attention} need attention`}
          </Text>
          <Button text="Manage machines" press={props.openMachines} />
        </Row>
      </Card>
      {state.problem === null ? null : (
        <Banner tone="err" title="Could not save the change." body={state.problem} />
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
      <Card section="Notifications">
        <Switch
          label="Notify me when a run finishes, fails or pauses"
          checked={notifications.enabled}
          change={enabled => dispatch({type: 'change', change: {kind: 'notifications', enabled}})}
        />
      </Card>
    </Page>
  );
}
