import {describe, expect, it} from '@effect/vitest';
import {APPEARANCE_MODES, themesOf} from '@vibesys/ui/tokens';
import {act, type JSX, useEffect, useReducer} from 'react';
import {
  applyChange,
  defaultAppearance,
  emptyAddMachine,
  emptyState,
  initialAppearance,
  initialNavigation,
  type MachineRow,
  NAVIGATION_KINDS,
  type NavigationAction,
  type NavigationKind,
  navigationReducer,
} from '../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../testing/dom/app-builders.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {CostOverviewScreen} from '../cost-overview/index.js';
import {type AddMachineController, AddMachinePage, MachinesScreen} from '../machines/index.js';
import {OrchestratorApp} from '../orchestrator/app.js';
import {runTitle} from '../orchestrator/fleet-words.js';
import {SettingsScreen} from '../settings/settings-screen.js';
import {COST_OVERVIEW_WORDS} from '../test-display-clock.js';
import {FocusMemory, placeOf} from './focus-memory.js';
import {AppShell} from './shell.js';

installDom();

const scenario = generateScenario({seed: 3, subagents: 2, live: true});
const host = hostSnapshotOf({
  model: {data: scenario.changes.reduce(applyChange, emptyState), agentCursors: {}},
});
/** A second host that has said nothing yet, so the list offers a host filter. */
const quiet = hostSnapshotOf({key: 'ssh:other', label: 'Other host'});
const machines: readonly MachineRow[] = [
  {key: 'local', label: 'This machine', status: {kind: 'live', cause: null, staleForMs: null}},
  {
    key: 'ssh:long',
    label: `ssh:${'a-very-long-host-name-'.repeat(8)}`,
    status: {kind: 'stale', cause: null, staleForMs: 3 * 60_000},
  },
];
const addMachine: AddMachineController = {
  state: emptyAddMachine,
  type: () => undefined,
  pick: () => undefined,
  submit: () => undefined,
  reset: () => undefined,
};
/** The rail's destinations, in the order it lists them. */
const RAIL_ORDER = ['Runs', 'Cost', 'Machines', 'Settings'];

/** What the run is called on the list and in every trail. */
const run = host.model.data.runs[scenario.runId];
const TASK = run === undefined ? '' : runTitle(run.taskCard ?? null, run.task);

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
            {navigation.machines.screen === 'add' ? (
              <AddMachinePage
                origin={navigation.machines.origin}
                controls={addMachine}
                close={() => go({type: 'close-add-machine'})}
                openRuns={() => go({type: 'open-runs'})}
              />
            ) : navigation.destination === 'runs' ? (
              <OrchestratorApp
                hosts={[host, quiet]}
                nowMs={scenario.endMs}
                commands={appCommandsOf()}
                retryHost={() => undefined}
                retryCost={() => undefined}
                route={navigation.runs}
                fleet={navigation.fleet}
                go={go}
              />
            ) : navigation.destination === 'cost' ? (
              <CostOverviewScreen
                hosts={[]}
                words={COST_OVERVIEW_WORDS}
                open={() => undefined}
                retry={() => undefined}
              />
            ) : navigation.destination === 'machines' ? (
              <MachinesScreen
                rows={machines}
                addMachine={() => go({type: 'open-add-machine', origin: 'machines'})}
              />
            ) : (
              <SettingsScreen
                appearance={initialAppearance('light')}
                choose={() => undefined}
                viewer={{id: 'install-7', label: 'Ana'}}
                machines={{
                  count: machines.length,
                  manage: () => go({type: 'open-machines'}),
                  add: () => go({type: 'open-add-machine', origin: 'machines'}),
                }}
                diagnostics={{protocolVersion: '12'}}
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
  const [row] = screen.container.querySelectorAll<HTMLElement>('.vs-summary-card-press');
  if (row === undefined) throw new Error('no run listed');
  return row;
};

const open = () => {
  const log: NavigationAction[] = [];
  const screen = mount(<Window log={log} />);
  return {log, screen};
};

describe('the window shell', () => {
  it('has a rail with the app mark, then Runs, Cost and Machines, and Settings last at its foot', () => {
    const {screen} = open();
    expect(screen.control('Runs').getAttribute('aria-current')).toBe('page');
    for (const name of ['Cost', 'Machines', 'Settings']) {
      expect(screen.control(name).getAttribute('aria-current')).toBeNull();
    }
    // Each destination says its name under its icon; the app's mark is a picture, not a control.
    for (const name of RAIL_ORDER) expect(screen.control(name).textContent).toBe(name);
    const rail = screen.container.querySelector('nav[aria-label="Main"]');
    expect(rail?.querySelector('.vs-rail-mark [role="img"]')?.getAttribute('aria-label')).toBe(
      'VibeSys',
    );
    expect(rail?.querySelector('.vs-rail-mark button, .vs-rail-mark [tabindex]')).toBeNull();
    const named = [...(rail?.querySelectorAll('button') ?? [])].map(
      button => button.getAttribute('aria-label') ?? button.textContent,
    );
    expect(named).toEqual([...RAIL_ORDER]);
    // Settings alone is in the foot group, after the spacer that holds it at the rail's end.
    const foot = rail?.querySelector('.vs-rail-group[data-end]');
    expect([...(foot?.querySelectorAll('button') ?? [])].map(button => button.textContent)).toEqual(
      ['Settings'],
    );
    expect(foot?.previousElementSibling?.classList.contains('vs-spacer')).toBe(true);
    screen.unmount();
  });

  it.each([
    'Cost',
    'Machines',
    'Settings',
  ])('switches between the runs and %s with the rail, in both directions', name => {
    const {screen} = open();
    screen.press(screen.control(name));
    expect(screen.control(name).getAttribute('aria-current')).toBe('page');
    expect(screen.control('Runs').getAttribute('aria-current')).toBeNull();
    expect(screen.container.querySelector('h1')?.textContent).toBe(name);
    screen.press(screen.control('Runs'));
    expect(screen.control('Runs').getAttribute('aria-current')).toBe('page');
    expect(screen.control(name).getAttribute('aria-current')).toBeNull();
    expect(screen.container.querySelector('h1')?.textContent).not.toBe(name);
    screen.unmount();
  });

  it('shows the Cost page as its name alone, and Settings as the appearance alone', () => {
    const {screen} = open();
    screen.press(screen.control('Cost'));
    expect(screen.container.querySelector('main')?.textContent).toBe('Cost');
    screen.press(screen.control('Settings'));
    expect(screen.text()).toContain('Light theme');
    expect(screen.text()).not.toContain('Resources');
    expect(screen.container.querySelector('nav[aria-label="Settings sections"]')).toBeNull();
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

  it('opens the Machines list and its dedicated Add flow from Settings', () => {
    const {screen, log} = open();
    screen.press(screen.control('Settings'));
    screen.press(screen.control('Manage machines'));
    expect(screen.container.querySelector('h1')?.textContent).toBe('Machines');

    screen.press(screen.control('Settings'));
    screen.press(screen.control('Add machine or server'));
    expect(screen.container.querySelector('h1')?.textContent).toBe('Add machine or server');
    screen.press(screen.control('Cancel'));
    expect(screen.container.querySelector('h1')?.textContent).toBe('Machines');
    expect(log.slice(-5).map(action => action.type)).toEqual([
      'open-settings',
      'open-machines',
      'open-settings',
      'open-add-machine',
      'close-add-machine',
    ]);
    screen.unmount();
  });

  it('keeps Add machine in the New run trail and restores keyboard focus when it closes', () => {
    const {screen} = open();
    screen.press(screen.control('New run'));
    const opener = screen.control('Add machine or server');
    act(() => opener.focus());
    screen.press(opener);

    expect(screen.container.querySelector('h1')?.textContent).toBe('Add machine or server');
    const rail = screen.container.querySelector('nav[aria-label="Main"]');
    const railControl = (name: string): HTMLElement => {
      const found = [...(rail?.querySelectorAll<HTMLElement>('button') ?? [])].find(
        control => control.textContent === name,
      );
      if (found === undefined) throw new Error(`the rail has no ${name} control`);
      return found;
    };
    expect(railControl('Runs').getAttribute('aria-current')).toBe('page');
    expect(railControl('Machines').getAttribute('aria-current')).toBeNull();
    expect(screen.container.querySelector('nav[aria-label="Where you are"]')?.textContent).toBe(
      'Runs/New run/Add machine or server',
    );

    screen.press(screen.control('Cancel'));
    expect(screen.container.querySelector('h1')?.textContent).toBe('New run');
    expect(
      document.activeElement?.getAttribute('aria-label') ?? document.activeElement?.textContent,
    ).toBe('Add machine or server');
    screen.unmount();
  });

  it('the Runs breadcrumb of an open run returns to the list', () => {
    const {screen, log} = open();
    screen.press(firstRun(screen));
    const crumb = screen.controls('Runs').find(control => control.classList.contains('vs-crumb'));
    if (crumb === undefined) throw new Error('the run has no Runs breadcrumb');
    screen.press(crumb);
    expect(log.map(action => action.type)).toEqual(['open-run', 'close-run']);
    expect(screen.container.querySelectorAll('.vs-summary-card-press').length).toBeGreaterThan(0);
    screen.unmount();
  });

  it('opens the Catch me up page from the run header and sends no press of its own', () => {
    const {screen, log} = open();
    screen.press(firstRun(screen));
    screen.press(screen.control('Catch me up'));
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
    const rowName = row.getAttribute('aria-label');
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
    expect(document.activeElement?.classList.contains('vs-summary-card-press')).toBe(true);
    expect(document.activeElement?.getAttribute('aria-label')).toBe(rowName);
    screen.unmount();
  });

  it('every navigation intent but the keyboard back and the ideas link is sent by a control the screens show', () => {
    const {screen, log} = open();
    const sent = (): Set<NavigationKind> => new Set(log.map(action => action.type));
    const row = (): HTMLElement => firstRun(screen);
    const search = screen.container.querySelector<HTMLInputElement>('input[type="search"]');
    if (search === null) throw new Error('the list has no search control');
    const SearchView = search.ownerDocument.defaultView as unknown as {Event: typeof Event};
    const setSearch = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(search) as object,
      'value',
    )?.set;
    if (setSearch === undefined) throw new Error('the search has no value setter');
    act(() => {
      search.focus();
      setSearch.call(search, scenario.runId);
      for (const name of ['input', 'change', 'keyup'])
        search.dispatchEvent(new SearchView.Event(name, {bubbles: true}));
    });
    act(() => {
      row()
        .closest('article')
        ?.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));
    });
    screen.press(screen.control('Mark as read'));
    act(() => {
      row()
        .closest('article')
        ?.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));
    });
    screen.press(screen.control('Mark as unread'));
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
    screen.press(screen.control('Cost'));
    screen.press(screen.control('Machines'));
    screen.press(screen.control('Add machine or server'));
    screen.press(screen.control('Cancel'));
    screen.press(screen.control('Settings'));
    screen.press(screen.control('Runs'));
    // The list's New run opens the page that starts a run; the rail's Runs comes back to the list.
    screen.press(screen.control('New run'));
    screen.press(
      screen.controls('Runs').find(control => !control.classList.contains('vs-crumb')) ?? row(),
    );
    // The list's headers sort it, and the edge of a column moves with the arrow keys.
    screen.press(screen.control('Sort by Cost'));
    const edge = screen.container.querySelector<HTMLElement>('hr.vs-columnlist-grip');
    if (edge === null) throw new Error('the list has no column edge');
    const Key = (edge.ownerDocument.defaultView as unknown as {KeyboardEvent: typeof KeyboardEvent})
      .KeyboardEvent;
    act(() => {
      edge.dispatchEvent(new Key('keydown', {key: 'ArrowRight', bubbles: true, cancelable: true}));
    });
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
  const settings = () =>
    mount(<SettingsScreen appearance={initialAppearance('dark')} choose={() => undefined} />);

  it('says which theme group is in use: the one of the mode the page is in', () => {
    const inUse = (state: ReturnType<typeof initialAppearance>) => {
      const screen = mount(<SettingsScreen appearance={state} choose={() => undefined} />);
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
    const screen = settings();
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
});

describe('the machines page', () => {
  it('shows each host as a card with its connection and one freshness status', () => {
    const screen = mount(<MachinesScreen rows={machines} />);
    const names = [...screen.container.querySelectorAll<HTMLElement>('.vs-card-title')];
    expect(names.map(title => title.textContent)).toEqual(machines.map(row => row.label));
    expect(screen.text()).toContain('Live');
    expect(screen.text()).toContain('Stale 3m');
    expect(screen.text()).not.toContain('Freshness');
    screen.unmount();
  });

  it('shows an empty list as a table with one quiet row, not as an error', () => {
    const screen = mount(<MachinesScreen rows={[]} />);
    const rows = [...screen.container.querySelectorAll('tbody tr')];
    expect(rows.map(row => row.textContent)).toEqual(['No machines']);
    screen.unmount();
  });
});
