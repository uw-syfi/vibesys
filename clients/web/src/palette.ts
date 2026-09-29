/** The ⌘K palette: every item is a control that is also visible somewhere in the window. */
import type {ProjectSection} from './home.js';
import type {RunControl} from './model.js';
import type {RoundRow} from './rounds.js';
import {THEME_LABELS, THEMES, type ThemeChoice} from './theme.js';
import type {PaneTab, UiAction} from './ui-state.js';

export type Intent =
  | {kind: 'ui'; action: UiAction}
  | {kind: 'toggleRun'}
  | {kind: 'copyRunId'}
  | {kind: 'steer'}
  | {kind: 'sidebar'}
  | {kind: 'reveal'; key: string; turn: string}
  | {kind: 'newRun'}
  | {kind: 'open'; href: string}
  | {kind: 'newThread'}
  | {kind: 'askMenu'; menu: 'thread' | 'model'}
  | {kind: 'theme'; theme: ThemeChoice};

export interface PaletteItem {
  id: string;
  group: 'Run' | 'Go to' | 'Agent' | 'Ask' | 'Appearance';
  label: string;
  detail: string;
  intent: Intent;
  /** The item's own keyboard shortcut, shown at the row's end. */
  keys?: string;
}

export interface PaletteInput {
  control: RunControl;
  /** A transition is pending: Pause/Resume is hidden in the title row, so here too. */
  pending: boolean;
  canStop: boolean;
  canSteer: boolean;
  hasRunId: boolean;
  rows: readonly RoundRow[];
  live: number | null;
  /** The round on screen and the open tab: going to either would do nothing. */
  selected: number | null;
  pane: PaneTab | null;
  sidebarShown: boolean;
  /** The page came from the home server: the sidebar shows New run. */
  newRun: boolean;
  /** The page came from the home server and the run has ended: null hides Resume run…. */
  resumeHref: string | null;
  /** The selected round's latest turn that recorded a prompt. */
  prompt: {turn: string; detail: string} | null;
  /** The selected round's latest turn that recorded todos. */
  todos: {turn: string; detail: string} | null;
  /** Ask's thread count and model; null while the run offers no chat harness (Ask shows no such controls). */
  ask: {threads: number; model: string | null} | null;
  theme: ThemeChoice;
}

const TABS: ReadonlyArray<readonly [PaneTab, string, string]> = [
  ['ask', 'Ask', 'chat about this run'],
  ['changes', 'Changes', 'selected round'],
  ['agents', 'Agents', 'invocation graph'],
  ['experiments', 'Experiments', 'hypotheses and performance'],
  ['notes', 'Notes', 'run notes'],
];

const item = (
  id: string,
  group: PaletteItem['group'],
  label: string,
  detail: string,
  intent: Intent,
): PaletteItem => ({
  id,
  group,
  label,
  detail,
  intent,
});

function runItems(input: PaletteInput): PaletteItem[] {
  const items: PaletteItem[] = [];
  const {control} = input;
  if (control.kind === 'action' && !control.disabled && !input.pending) {
    const label =
      control.action === 'pause' ? 'Pause after the current agent call' : 'Resume the run';
    items.push(item('run-toggle', 'Run', label, '', {kind: 'toggleRun'}));
  }
  if (input.canStop) {
    items.push(
      item('run-stop', 'Run', 'Stop run…', 'asks first', {
        kind: 'ui',
        action: {type: 'menu', menu: 'stop'},
      }),
    );
  }
  if (input.canSteer)
    items.push(
      item('run-steer', 'Run', 'Steer the next agent call', 'focus the composer', {kind: 'steer'}),
    );
  if (input.hasRunId) items.push(item('run-copy', 'Run', 'Copy run ID', '', {kind: 'copyRunId'}));
  if (input.resumeHref !== null) {
    items.push(
      item('run-resume', 'Run', 'Resume run…', '', {kind: 'open', href: input.resumeHref}),
    );
  }
  if (input.newRun)
    items.push({...item('run-new', 'Run', 'New run', '', {kind: 'newRun'}), keys: '⌘N'});
  return items;
}

function goToItems(input: PaletteInput): PaletteItem[] {
  const rounds = input.rows
    .filter(row => row.round !== input.selected)
    .map(row =>
      item(`round-${row.round}`, 'Go to', `Round ${row.round}`, row.title ?? '', {
        kind: 'ui',
        action: {type: 'round', round: row.round, live: input.live},
      }),
    );
  const tabs = TABS.filter(([pane]) => pane !== input.pane).map(([pane, label, detail]) =>
    item(`pane-${pane}`, 'Go to', label, detail, {kind: 'ui', action: {type: 'pane', pane}}),
  );
  const sidebar = item(
    'sidebar',
    'Go to',
    input.sidebarShown ? 'Hide sidebar' : 'Show sidebar',
    '',
    {kind: 'sidebar'},
  );
  return [...rounds, ...tabs, sidebar];
}

function agentItems(input: PaletteInput): PaletteItem[] {
  const items: PaletteItem[] = [];
  if (input.prompt !== null) {
    const {turn, detail} = input.prompt;
    items.push(
      item('agent-prompt', 'Agent', 'Show the prompt', detail, {
        kind: 'reveal',
        key: `${turn}:prompt`,
        turn,
      }),
    );
  }
  if (input.todos !== null) {
    const {turn, detail} = input.todos;
    items.push(
      item('agent-todos', 'Agent', 'Show the todos', detail, {
        kind: 'reveal',
        key: `${turn}:todos`,
        turn,
      }),
    );
  }
  return items;
}

function askItems(input: PaletteInput): PaletteItem[] {
  if (input.ask === null) return [];
  const {threads, model} = input.ask;
  return [
    item('ask-new', 'Ask', 'New thread', '', {kind: 'newThread'}),
    item('ask-thread', 'Ask', 'Switch thread…', threads === 1 ? '1 thread' : `${threads} threads`, {
      kind: 'askMenu',
      menu: 'thread',
    }),
    item('ask-model', 'Ask', 'Chat model…', model ?? '', {kind: 'askMenu', menu: 'model'}),
  ];
}

function themeItems(theme: ThemeChoice): PaletteItem[] {
  return THEMES.map(choice =>
    item(
      `theme-${choice}`,
      'Appearance',
      `Theme: ${THEME_LABELS[choice]}`,
      choice === theme ? 'current' : '',
      {
        kind: 'theme',
        theme: choice,
      },
    ),
  );
}

export function paletteItems(input: PaletteInput): PaletteItem[] {
  return [
    ...runItems(input),
    ...goToItems(input),
    ...agentItems(input),
    ...askItems(input),
    ...themeItems(input.theme),
  ];
}

/** The home page's palette: New run (unless it is open), every run the sidebar links, and Appearance. */
export function homePaletteItems(
  sections: readonly ProjectSection[],
  newRun: boolean,
  theme: ThemeChoice,
): PaletteItem[] {
  const runs = sections.flatMap(section =>
    section.runs.flatMap(run =>
      run.url === null
        ? []
        : [
            item(`open-${run.projectId}-${run.id}`, 'Go to', run.title, section.name, {
              kind: 'open',
              href: run.url,
            }),
          ],
    ),
  );
  const create = {...item('run-new', 'Run', 'New run', '', {kind: 'newRun'}), keys: '⌘N'};
  return [...(newRun ? [create] : []), ...runs, ...themeItems(theme)];
}

export function filterPalette(items: readonly PaletteItem[], query: string): PaletteItem[] {
  const needle = query.trim().toLowerCase();
  if (needle === '') return [...items];
  return items.filter(entry =>
    `${entry.group}: ${entry.label} ${entry.detail}`.toLowerCase().includes(needle),
  );
}
