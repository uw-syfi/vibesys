/** The ⌘K palette: every item is a control that is also visible somewhere in the window. */
import type {RunControl} from './model.js';
import type {RoundRow} from './rounds.js';
import type {PaneTab, UiAction} from './ui-state.js';

export type Intent =
  | {kind: 'ui'; action: UiAction}
  | {kind: 'toggleRun'}
  | {kind: 'copyRunId'}
  | {kind: 'steer'}
  | {kind: 'sidebar'}
  | {kind: 'reveal'; key: string; turn: string};

export interface PaletteItem {
  id: string;
  group: 'Run' | 'Go to' | 'Agent';
  label: string;
  detail: string;
  intent: Intent;
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
  /** The selected round's latest turn that recorded a prompt. */
  prompt: {turn: string; detail: string} | null;
  /** The selected round's latest turn that recorded todos. */
  todos: {turn: string; detail: string} | null;
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

export function paletteItems(input: PaletteInput): PaletteItem[] {
  return [...runItems(input), ...goToItems(input), ...agentItems(input)];
}

export function filterPalette(items: readonly PaletteItem[], query: string): PaletteItem[] {
  const needle = query.trim().toLowerCase();
  if (needle === '') return [...items];
  return items.filter(entry =>
    `${entry.group}: ${entry.label} ${entry.detail}`.toLowerCase().includes(needle),
  );
}
