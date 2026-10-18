import {
  Banner,
  Button,
  Card,
  Grid,
  Page,
  Row,
  SectionLayout,
  SegmentedControl,
  SidebarItem,
  Stack,
  Switch,
  Text,
  ThemeSwatch,
} from '@vibesys/ui';
import {type Appearance, type AppearanceMode, type ThemeName, themesOf} from '@vibesys/ui/tokens';
import {type JSX, type ReactNode, useState} from 'react';
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

type Section = 'appearance' | 'machines' | 'notifications';

/** Settings sections beside their content, with Machines supplied by the shell that owns it. */
export function SettingsScreen(props: {
  readonly state: SettingsState;
  readonly dispatch: (action: SettingsAction) => void;
  readonly back: () => void;
  readonly machinesContent: ReactNode;
  /** The Machines section's own count; while it is not `listed`, no count is worded. */
  readonly machines: {
    readonly count: number;
    readonly attention: number;
    readonly listed: boolean;
  };
}): JSX.Element {
  const {state, dispatch} = props;
  const [section, setSection] = useState<Section>('appearance');
  const choose = (next: Appearance): void =>
    dispatch({type: 'change', change: {kind: 'appearance', appearance: next}});
  const content = (): JSX.Element => {
    if (section === 'machines') return <>{props.machinesContent}</>;
    if (state.kind === 'loading') return <Text variant="body">Loading settings…</Text>;
    const {appearance, notifications} = state.shown;
    if (section === 'notifications') {
      return (
        <Stack>
          <Text as="h1" variant="title">
            Notifications
          </Text>
          {state.problem === null ? null : (
            <Banner tone="err" title="Could not save the change." body={state.problem} />
          )}
          <Card section="Run notifications">
            <Switch
              label="Notify me when a run finishes, fails or pauses"
              checked={notifications.enabled}
              change={enabled =>
                dispatch({type: 'change', change: {kind: 'notifications', enabled}})
              }
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
      </Stack>
    );
  };
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
        sections={
          <>
            <SidebarItem
              label="Appearance"
              active={section === 'appearance'}
              press={() => setSection('appearance')}
            />
            <SidebarItem
              label="Machines"
              {...(props.machines.listed
                ? {
                    detail: `${props.machines.count} saved${props.machines.attention === 0 ? '' : ` · ${props.machines.attention} need attention`}`,
                  }
                : {})}
              active={section === 'machines'}
              press={() => setSection('machines')}
            />
            <SidebarItem
              label="Notifications"
              active={section === 'notifications'}
              press={() => setSection('notifications')}
            />
          </>
        }
      >
        {content()}
      </SectionLayout>
    </Page>
  );
}
