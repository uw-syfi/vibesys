import {DEFAULT_CHAT_THREAD_ID} from '@vibesys/core-state';

/** What the window shows: selection, pane, widths, disclosures, menus. A pure reducer. */
export type PaneTab = 'ask' | 'changes' | 'agents' | 'experiments' | 'notes';
/** The one open popover: the ••• menu, the Stop confirmation, or Ask's thread and model menus. */
export type Menu = 'more' | 'stop' | 'thread' | 'model' | null;

export interface UiState {
  /** The run the selection below belongs to; a different run starts from a clean selection. */
  runId: string | null;
  /** The picked round; null follows the live round. */
  round: number | null;
  pane: PaneTab | null;
  sidebar: boolean;
  sideWidth: number;
  paneWidth: number;
  /** The tool row whose output is open. */
  expanded: string | null;
  /** `<turn id>:prompt` and `<turn id>:todos` disclosures. */
  disclosed: Readonly<Record<string, boolean>>;
  /** The execution the transcript is filtered to; read it through `agentFilter`. */
  agent: string | null;
  /** The round the filter was set in: it lapses once another round shows. */
  agentRound: number | null;
  menu: Menu;
  palette: boolean;
  /** The experiments row whose evidence is open. */
  evidence: number | null;
  experimentsView: 'hypotheses' | 'design';
  /** The Ask thread on screen. */
  thread: string;
  /** Unsent composer text; a note can replace either (drafts only, nothing is sent). */
  drafts: {steer: string; ask: string};
}

export type UiAction =
  | {type: 'run'; runId: string | null}
  | {type: 'round'; round: number; live: number | null}
  | {type: 'pane'; pane: PaneTab | null}
  | {type: 'togglePane'}
  | {type: 'sidebar'; open: boolean}
  | {type: 'resize'; target: 'side' | 'pane'; width: number}
  | {type: 'expand'; id: string}
  | {type: 'disclose'; key: string; open?: boolean}
  | {type: 'agent'; id: string | null; round: number | null}
  | {type: 'menu'; menu: Menu}
  | {type: 'palette'; open: boolean}
  | {type: 'evidence'; round: number}
  | {type: 'experimentsView'; view: 'hypotheses' | 'design'}
  | {type: 'thread'; id: string}
  | {type: 'draft'; target: 'steer' | 'ask'; text: string};

export const SIDE = {min: 220, max: 380, initial: 276} as const;
export const PANE = {min: 340, max: 640, initial: 400} as const;

export const INITIAL_UI: UiState = {
  runId: null,
  round: null,
  pane: null,
  sidebar: true,
  sideWidth: SIDE.initial,
  paneWidth: PANE.initial,
  expanded: null,
  disclosed: {},
  agent: null,
  agentRound: null,
  menu: null,
  palette: false,
  evidence: null,
  experimentsView: 'hypotheses',
  thread: DEFAULT_CHAT_THREAD_ID,
  drafts: {steer: '', ask: ''},
};

/**
 * The agent filter for the round on screen. A filter belongs to the round it was set in, so it
 * ends when the reader picks another round or the followed live round advances.
 */
export function agentFilter(ui: UiState, round: number | null): string | null {
  return ui.agent !== null && ui.agentRound === round ? ui.agent : null;
}

const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value));

/**
 * The state as it applies to `runId`: selection, open rows, disclosures, the agent filter, open
 * evidence, menus, the palette, the Ask thread and drafts belong to one run and reset when the
 * session replaces it (a reconnect that lands on a new run). Layout (pane, sidebar, widths, view)
 * carries over.
 */
export function forRun(state: UiState, runId: string | null): UiState {
  if (state.runId === runId) return state;
  return {
    ...state,
    runId,
    round: null,
    expanded: null,
    disclosed: {},
    agent: null,
    agentRound: null,
    menu: null,
    palette: false,
    evidence: null,
    thread: DEFAULT_CHAT_THREAD_ID,
    drafts: INITIAL_UI.drafts,
  };
}

/** Picking the filtered execution again (in the same round) clears the filter. */
function toggleAgent(state: UiState, id: string | null, round: number | null): UiState {
  const agent = state.agent === id && state.agentRound === round ? null : id;
  return {...state, agent, agentRound: agent === null ? null : round};
}

export function uiReducer(state: UiState, action: UiAction): UiState {
  switch (action.type) {
    case 'run':
      return forRun(state, action.runId);
    case 'round':
      return {
        ...state,
        round: action.round === action.live ? null : action.round,
        expanded: null,
        agent: null,
        agentRound: null,
      };
    case 'pane':
      return {...state, pane: action.pane, menu: null};
    case 'togglePane':
      return {...state, pane: state.pane === null ? 'changes' : null};
    case 'sidebar':
      return {...state, sidebar: action.open};
    case 'resize':
      return action.target === 'side'
        ? {...state, sideWidth: clamp(action.width, SIDE.min, SIDE.max)}
        : {...state, paneWidth: clamp(action.width, PANE.min, PANE.max)};
    case 'expand':
      return {...state, expanded: state.expanded === action.id ? null : action.id};
    case 'disclose':
      return {
        ...state,
        disclosed: {...state.disclosed, [action.key]: action.open ?? !state.disclosed[action.key]},
      };
    case 'agent':
      return toggleAgent(state, action.id, action.round);
    case 'menu':
      return {...state, menu: action.menu};
    case 'palette':
      return {...state, palette: action.open, menu: null};
    case 'evidence':
      return {...state, evidence: state.evidence === action.round ? null : action.round};
    case 'experimentsView':
      return {...state, experimentsView: action.view};
    case 'thread':
      return {...state, thread: action.id, menu: null};
    case 'draft':
      return {...state, drafts: {...state.drafts, [action.target]: action.text}};
  }
}

const TRANSCRIPT_MIN = 560;

/**
 * Which of the sidebar and the pane fit at `width`: the pane keeps at least its minimum, and the
 * sidebar yields before the transcript drops below `TRANSCRIPT_MIN` (at 1024 with a pane open).
 */
export function frame(width: number, ui: UiState): {sidebar: boolean; paneWidth: number} {
  const paneWidth =
    ui.pane === null ? 0 : Math.max(PANE.min, Math.min(ui.paneWidth, width - TRANSCRIPT_MIN));
  const room = width - paneWidth - (ui.sidebar ? ui.sideWidth : 0);
  return {sidebar: ui.sidebar && room >= TRANSCRIPT_MIN, paneWidth};
}
