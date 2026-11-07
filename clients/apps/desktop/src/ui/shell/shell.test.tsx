import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {BuildVersionsSchema} from '@vibesys/orchestrator-proto';
import {APPEARANCE_MODES, themesOf} from '@vibesys/ui/tokens';
import {type JSX, useReducer} from 'react';
import {
  applyChange,
  type CommandProgress,
  defaultAppearance,
  emptyState,
  initialAppearance,
  initialNavigation,
  NAVIGATION_KINDS,
  type NavigationAction,
  type NavigationKind,
  navigationReducer,
  type ResourceRow,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {type HostSnapshot, OrchestratorApp} from '../orchestrator/app.js';
import {SettingsScreen} from '../settings/settings-screen.js';
import {AppShell} from './shell.js';

installDom();

const scenario = generateScenario({seed: 3, subagents: 2, live: true});
const host: HostSnapshot = {
  key: 'local',
  label: 'This machine',
  model: {data: scenario.changes.reduce(applyChange, emptyState), agentCursors: {}},
  connection: {_tag: 'Ready', epoch: 1, daemon: create(BuildVersionsSchema)},
  runsFeed: {_tag: 'Live'},
  agentFeeds: {},
  costFeeds: {},
};
const resources: readonly ResourceRow[] = [
  {key: 'local', label: 'This machine', connection: 'connected', freshness: 'live'},
  {
    key: 'ssh:long',
    label: `ssh:${'a-very-long-host-name-'.repeat(8)}`,
    connection: 'reconnecting',
    freshness: 'stale',
  },
];
const idle: CommandProgress | undefined = undefined;

/** The window as the entry composes it, over the navigation fold, recording every intent sent. */
function Window(props: {readonly log: NavigationAction[]}): JSX.Element {
  const [navigation, move] = useReducer(navigationReducer, initialNavigation);
  const go = (action: NavigationAction): void => {
    props.log.push(action);
    move(action);
  };
  return (
    <AppShell destination={navigation.destination} go={go}>
      {navigation.destination === 'runs' ? (
        <OrchestratorApp
          hosts={[host]}
          nowMs={scenario.endMs}
          commands={{
            start: idle,
            stop: {},
            send: {},
            startRun: () => undefined,
            retryStart: () => undefined,
            stopRun: () => undefined,
            retryStop: () => undefined,
            sendMessage: () => undefined,
            retrySend: () => undefined,
          }}
          retryHost={() => undefined}
          retryCost={() => undefined}
          route={navigation.runs}
          go={go}
        />
      ) : (
        <SettingsScreen
          tab={navigation.settingsTab}
          pick={tab => go({type: 'pick-settings-tab', tab})}
          appearance={initialAppearance('light')}
          choose={() => undefined}
          resources={resources}
        />
      )}
    </AppShell>
  );
}

const open = () => {
  const log: NavigationAction[] = [];
  const screen = mount(<Window log={log} />);
  return {log, screen};
};

describe('the window shell', () => {
  it('has a rail with the app mark, Runs and Settings, icon only, Runs current at first', () => {
    const {screen} = open();
    expect(screen.control('Runs').getAttribute('aria-current')).toBe('page');
    expect(screen.control('Settings').getAttribute('aria-current')).toBeNull();
    expect(screen.control('VibeSys')).toBeDefined();
    // Icon only: the buttons carry no visible words.
    for (const name of ['Runs', 'Settings', 'VibeSys'])
      expect(screen.control(name).textContent).toBe('');
    screen.unmount();
  });

  it('switches between the runs and the settings with the rail, in both directions', () => {
    const {screen} = open();
    screen.press(screen.control('Settings'));
    expect(screen.control('Settings').getAttribute('aria-current')).toBe('page');
    expect(screen.control('Runs').getAttribute('aria-current')).toBeNull();
    expect(screen.text()).toContain('Appearance');
    screen.press(screen.control('Runs'));
    expect(screen.control('Runs').getAttribute('aria-current')).toBe('page');
    expect(screen.text()).not.toContain('Appearance');
    screen.unmount();
  });

  it('returns to the open run when Settings is left', () => {
    const {screen} = open();
    const [row] = screen.container.querySelectorAll<HTMLElement>('tr[data-selectable]');
    if (row === undefined) throw new Error('no run listed');
    screen.press(row);
    expect(screen.controls('Runs').length).toBeGreaterThan(1);
    screen.press(screen.control('Settings'));
    screen.press(screen.control('Runs'));
    expect(screen.controls('Timeline')).toHaveLength(1);
    screen.unmount();
  });

  it('the Runs breadcrumb of an open run returns to the list', () => {
    const {screen, log} = open();
    const [row] = screen.container.querySelectorAll<HTMLElement>('tr[data-selectable]');
    if (row === undefined) throw new Error('no run listed');
    screen.press(row);
    const crumb = screen.controls('Runs').find(control => control.classList.contains('vs-crumb'));
    if (crumb === undefined) throw new Error('the run has no Runs breadcrumb');
    screen.press(crumb);
    expect(log.map(action => action.type)).toEqual(['open-run', 'close-run']);
    expect(screen.container.querySelectorAll('tr[data-selectable]').length).toBeGreaterThan(0);
    screen.unmount();
  });

  it('every navigation intent but the keyboard back is sent by a control the screens show', () => {
    const {screen, log} = open();
    const sent = (): Set<NavigationKind> => new Set(log.map(action => action.type));
    const row = (): HTMLElement => {
      const [first] = screen.container.querySelectorAll<HTMLElement>('tr[data-selectable]');
      if (first === undefined) throw new Error('no run listed');
      return first;
    };
    screen.press(row());
    const [agent] = screen.container.querySelectorAll<HTMLElement>('[role="treeitem"]');
    if (agent === undefined) throw new Error('the run lists no agent');
    screen.press(agent);
    screen.press(screen.control('Close agent view'));
    screen.press(screen.control('Cost'));
    screen.press(
      screen.controls('Runs').find(control => control.classList.contains('vs-crumb')) ?? row(),
    );
    screen.press(screen.control('Settings'));
    screen.press(screen.control('Resources'));
    screen.press(screen.control('Runs'));
    expect(sent()).toEqual(new Set(NAVIGATION_KINDS.filter(kind => kind !== 'back')));
    screen.unmount();
  });
});

describe('the settings screen', () => {
  const settings = (overrides: {readonly tab: 'appearance' | 'resources'}) =>
    mount(
      <SettingsScreen
        tab={overrides.tab}
        pick={() => undefined}
        appearance={initialAppearance('dark')}
        choose={() => undefined}
        resources={resources}
      />,
    );

  it('lists only tabs with content: Appearance and Resources', () => {
    const screen = settings({tab: 'appearance'});
    const tabs = [
      ...screen.container.querySelectorAll('nav[aria-label="Settings sections"] button'),
    ];
    expect(tabs.map(tab => tab.textContent)).toEqual(['Appearance', 'Resources']);
    screen.unmount();
  });

  it('says which theme group is in use: the one of the mode the page is in', () => {
    const inUse = (state: ReturnType<typeof initialAppearance>) => {
      const screen = mount(
        <SettingsScreen
          tab="appearance"
          pick={() => undefined}
          appearance={state}
          choose={() => undefined}
          resources={resources}
        />,
      );
      const groups = [...screen.container.querySelectorAll('.vs-chip')]
        .filter(chip => chip.textContent === 'In use' && chip.closest('.vs-reserve') === null)
        .map(chip => chip.parentElement?.textContent);
      screen.unmount();
      return groups;
    };
    expect(inUse(initialAppearance('light'))).toEqual(['Light themeIn use']);
    expect(inUse(initialAppearance('dark'))).toEqual(['Dark themeIn use']);
    const forcedDark = {
      ...initialAppearance('light'),
      appearance: {...defaultAppearance, mode: 'dark' as const},
    };
    expect(inUse(forcedDark)).toEqual(['Dark themeIn use']);
  });

  it('offers every mode and every theme of the design system, the saved ones selected', () => {
    const screen = settings({tab: 'appearance'});
    for (const mode of APPEARANCE_MODES) {
      const label = `${mode.charAt(0).toUpperCase()}${mode.slice(1)}`;
      expect(screen.container.querySelectorAll(`label[data-label="${label}"]`).length).toBe(1);
    }
    for (const theme of [...themesOf('light'), ...themesOf('dark')]) {
      const label = `${theme.charAt(0).toUpperCase()}${theme.slice(1)}`;
      expect(screen.controls(label).length).toBe(1);
    }
    const selected = [...screen.container.querySelectorAll('[aria-pressed="true"]')].map(
      each => each.textContent,
    );
    expect(selected).toEqual(['Paper', 'Ink'].map(name => name));
    expect(defaultAppearance.light).toBe('paper');
    screen.unmount();
  });

  it('shows each host with its connection and how fresh it is, a long name cut short with the whole on hover', () => {
    const screen = settings({tab: 'resources'});
    const names = [...screen.container.querySelectorAll<HTMLElement>('td[data-clip]')];
    expect(names.map(cell => cell.textContent)).toEqual(resources.map(row => row.label));
    expect(names.map(cell => cell.getAttribute('title'))).toEqual(resources.map(row => row.label));
    expect(screen.text()).toContain('Connected');
    expect(screen.text()).toContain('Reconnecting');
    expect(screen.text()).toContain('Stale');
    screen.unmount();
  });

  it('shows an empty list as a table with no rows, not as an error', () => {
    const screen = mount(
      <SettingsScreen
        tab="resources"
        pick={() => undefined}
        appearance={initialAppearance('dark')}
        choose={() => undefined}
        resources={[]}
      />,
    );
    expect(screen.container.querySelectorAll('tbody tr').length).toBe(0);
    screen.unmount();
  });
});
