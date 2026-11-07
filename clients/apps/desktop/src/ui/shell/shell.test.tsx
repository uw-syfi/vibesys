import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {BuildVersionsSchema} from '@vibesys/orchestrator-proto';
import {APPEARANCE_MODES, themesOf} from '@vibesys/ui/tokens';
import {act, type JSX, useEffect, useReducer} from 'react';
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
import {FocusMemory, placeOf} from './focus-memory.js';
import {AppShell} from './shell.js';

installDom();

const scenario = generateScenario({seed: 3, subagents: 2, live: true});
const host: HostSnapshot = {
  key: 'local',
  label: 'This machine',
  model: {data: scenario.changes.reduce(applyChange, emptyState), agentCursors: {}},
  connection: {_tag: 'Ready', epoch: 1, daemon: create(BuildVersionsSchema)},
  freshness: {status: 'live', lastUpdateMs: null},
  runsFeed: {_tag: 'Live'},
  agentFeeds: {},
  costFeeds: {},
  runHistory: {_tag: 'Idle'},
  history: {earlier: {_tag: 'Idle'}, wholes: {}},
};
/** A second host that has said nothing yet, so the list offers a host filter. */
const quiet: HostSnapshot = {
  key: 'ssh:other',
  label: 'Other host',
  model: {data: emptyState, agentCursors: {}},
  connection: {_tag: 'Ready', epoch: 1, daemon: create(BuildVersionsSchema)},
  freshness: {status: 'live', lastUpdateMs: null},
  runsFeed: {_tag: 'Live'},
  agentFeeds: {},
  costFeeds: {},
  runHistory: {_tag: 'Idle'},
  history: {earlier: {_tag: 'Idle'}, wholes: {}},
};
const resources: readonly ResourceRow[] = [
  {key: 'local', label: 'This machine', status: {kind: 'live', cause: null, staleForMs: null}},
  {
    key: 'ssh:long',
    label: `ssh:${'a-very-long-host-name-'.repeat(8)}`,
    status: {kind: 'stale', cause: null, staleForMs: 3 * 60_000},
  },
];
const idle: CommandProgress | undefined = undefined;
/** Every press of Catch me up the window sent, as `host/run`. */
const caughtUp: string[] = [];

/** What the run is called on the list and in every trail. */
const TASK = host.model.data.runs[scenario.runId]?.task ?? '';

/** The window as the entry composes it, over the navigation fold, recording every intent sent. */
function Window(props: {readonly log: NavigationAction[]}): JSX.Element {
  const [navigation, move] = useReducer(navigationReducer, initialNavigation);
  // The window's keyboard: Escape is one step back, as the entry's key press stream makes it.
  useEffect(() => {
    const listen = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') move({type: 'back'});
    };
    document.addEventListener('keydown', listen);
    return () => document.removeEventListener('keydown', listen);
  }, []);
  return (
    <FocusMemory place={placeOf(navigation)}>
      {note => {
        const go = (action: NavigationAction): void => {
          props.log.push(action);
          // As the shell does: what opens a page keeps its opener, so going back finds it again.
          note(action);
          move(action);
        };
        return (
          <AppShell destination={navigation.destination} go={go}>
            {navigation.destination === 'runs' ? (
              <OrchestratorApp
                hosts={[host, quiet]}
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
                  catchUp: (host, runId) => caughtUp.push(`${host}/${runId}`),
                  loadEarlier: () => undefined,
                  expand: () => undefined,
                }}
                retryHost={() => undefined}
                retryCost={() => undefined}
                route={navigation.runs}
                fleet={navigation.fleet}
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
      }}
    </FocusMemory>
  );
}

/** The first run the list shows, as a person would press it. */
const firstRun = (screen: ReturnType<typeof mount>): HTMLElement => {
  const [row] = screen.container.querySelectorAll<HTMLElement>('[role="option"]');
  if (row === undefined) throw new Error('no run listed');
  return row;
};

const open = () => {
  caughtUp.length = 0;
  const log: NavigationAction[] = [];
  const screen = mount(<Window log={log} />);
  return {log, screen};
};

describe('the window shell', () => {
  it('has a rail with the app mark, Runs and Settings labelled under their icons, Runs current at first', () => {
    const {screen} = open();
    expect(screen.control('Runs').getAttribute('aria-current')).toBe('page');
    expect(screen.control('Settings').getAttribute('aria-current')).toBeNull();
    expect(screen.control('VibeSys')).toBeDefined();
    // Each destination says its name under its icon; the app's mark is the icon alone.
    for (const name of ['Runs', 'Settings']) expect(screen.control(name).textContent).toBe(name);
    expect(screen.control('VibeSys').textContent).toBe('');
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
    screen.press(firstRun(screen));
    expect(screen.controls('Runs').length).toBeGreaterThan(1);
    screen.press(screen.control('Settings'));
    screen.press(screen.control('Runs'));
    // The run opens on its report, whose agents card is the way to the agents.
    expect(screen.controls('Open agents')).toHaveLength(1);
    screen.unmount();
  });

  it('the Runs breadcrumb of an open run returns to the list', () => {
    const {screen, log} = open();
    screen.press(firstRun(screen));
    const crumb = screen.controls('Runs').find(control => control.classList.contains('vs-crumb'));
    if (crumb === undefined) throw new Error('the run has no Runs breadcrumb');
    screen.press(crumb);
    expect(log.map(action => action.type)).toEqual(['open-run', 'close-run']);
    expect(screen.container.querySelectorAll('[role="option"]').length).toBeGreaterThan(0);
    screen.unmount();
  });

  it('presses Catch me up on the open run and opens its page, from the run header', () => {
    const {screen, log} = open();
    screen.press(firstRun(screen));
    screen.press(screen.control('Catch me up'));
    expect(caughtUp).toHaveLength(1);
    expect(caughtUp[0]).toMatch(/^local\/run-/);
    expect(log.at(-1)).toEqual({type: 'show', view: 'catch-up'});
    screen.unmount();
  });

  it('lands the focus on the heading of a screen that opens, and on its opener when Escape goes back', () => {
    const {screen} = open();
    const pressEscape = (): void => {
      const View = document.defaultView as unknown as {KeyboardEvent: typeof KeyboardEvent};
      act(() => {
        document.dispatchEvent(new View.KeyboardEvent('keydown', {key: 'Escape'}));
      });
    };
    const focused = (): string =>
      `${document.activeElement?.tagName}:${document.activeElement?.getAttribute('aria-label') ?? document.activeElement?.textContent}`;
    // The list: a run opens on its report, with the focus on its heading.
    const row = firstRun(screen);
    act(() => row.focus());
    const rowName = row.textContent;
    screen.press(row);
    expect(focused()).toBe(`H1:${TASK}`);
    // The report's links open the cost and the agents, and Escape returns to each link.
    for (const link of ['Open cost', 'Open agents']) {
      act(() => screen.control(link).focus());
      screen.press(screen.control(link));
      expect(document.activeElement?.tagName).toBe('H1');
      expect(document.activeElement?.textContent).toBe(link === 'Open cost' ? 'Cost' : 'Agents');
      pressEscape();
      expect(focused()).toBe(`BUTTON:${link}`);
    }
    // An agent opens from its row on the agents page; Escape returns to that row, then to the link.
    act(() => screen.control('Open agents').focus());
    screen.press(screen.control('Open agents'));
    const [agent] = screen.container.querySelectorAll<HTMLElement>(
      '[role="listbox"] [role="option"]',
    );
    if (agent === undefined) throw new Error('the run lists no agent');
    const agentName = agent.getAttribute('aria-label');
    act(() => agent.focus());
    screen.press(agent);
    expect(document.activeElement?.tagName).toBe('H1');
    pressEscape();
    expect(document.activeElement?.getAttribute('aria-label')).toBe(agentName);
    pressEscape();
    expect(focused()).toBe('BUTTON:Open agents');
    // Back to the list: the focus is on the row the run was opened from.
    pressEscape();
    expect(document.activeElement?.getAttribute('role')).toBe('option');
    expect(document.activeElement?.textContent).toBe(rowName);
    screen.unmount();
  });

  it('every navigation intent but the keyboard back and the ideas link is sent by a control the screens show', () => {
    const {screen, log} = open();
    const sent = (): Set<NavigationKind> => new Set(log.map(action => action.type));
    const row = (): HTMLElement => firstRun(screen);
    screen.press(row());
    screen.press(screen.control('Open agents'));
    const [agent] = screen.container.querySelectorAll<HTMLElement>(
      '[role="listbox"] [role="option"]',
    );
    if (agent === undefined) throw new Error('the run lists no agent');
    screen.press(agent);
    // The agent's own page: its trail's Agents step closes it, the run's step goes to the report.
    screen.press(screen.control('Agents'));
    screen.press(screen.control(TASK));
    screen.press(screen.control('Open cost'));
    screen.press(
      screen.controls('Runs').find(control => control.classList.contains('vs-crumb')) ?? row(),
    );
    screen.press(screen.control('Settings'));
    screen.press(screen.control('Resources'));
    screen.press(screen.control('Runs'));
    // The list's own controls: its state filter and, with two hosts, its host filter.
    screen.press(
      screen.container.querySelector<HTMLElement>('input[type="radio"][value="live"]') ?? row(),
    );
    const hosts = screen.container.querySelector('select');
    if (hosts === null) throw new Error('the list has no host control');
    const View = hosts.ownerDocument.defaultView as unknown as {Event: typeof Event};
    act(() => {
      hosts.value = 'ssh:other';
      hosts.dispatchEvent(new View.Event('change', {bubbles: true}));
    });
    // The ideas tile of Catch me up sends show-ideas, and needs a stored check-in to draw: the catch-up
    // page's own tests press it.
    expect(sent()).toEqual(
      new Set(NAVIGATION_KINDS.filter(kind => kind !== 'back' && kind !== 'show-ideas')),
    );
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
    expect(screen.text()).toContain('Live');
    expect(screen.text()).toContain('Stale 3m');
    expect(screen.text()).not.toContain('Freshness');
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
