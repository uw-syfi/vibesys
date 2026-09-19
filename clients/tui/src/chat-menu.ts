import type {ChatOptions} from '@vibesys/backend-client';
import {type ChatMenuRow, chatThreadLabel, type SessionState} from './session-model.js';
import {agentRuntimeLabel} from './ui/agent-runtime-label.js';

/**
 * Reducers and selectors for the chat composer's inline menu.
 *
 * The state lives in `SessionState.chatMenu` (session-model.ts owns the
 * `ChatMenu` and `ChatMenuRow` shapes); everything that opens, fills,
 * navigates, or reads that menu is here, in the same pure style as the
 * session reducers. The client enumerates nothing: `/model` rows come from
 * the backend's `query.chat_options` response verbatim, and `/resume` rows
 * from the run's thread list.
 */

/** `/resume`: the thread list, as an inline selection on the active thread. */
export function openChatResumeMenu(state: SessionState): SessionState {
  const rows: ChatMenuRow[] = state.core.chatThreads.map(thread => ({
    kind: 'thread' as const,
    label: chatThreadLabel(state, thread.id),
    detail: agentRuntimeLabel(thread.provider, thread.model) ?? 'run agent',
    threadId: thread.id,
    active: thread.id === state.activeChatThreadId,
  }));
  const active = rows.findIndex(row => row.kind === 'thread' && row.active);
  return {
    ...state,
    overlay: null,
    themePicker: null,
    chatMenu: {
      kind: 'resume',
      title: 'Chat threads',
      rows,
      selected: active === -1 ? firstSelectable(rows) : active,
      pending: false,
      error: null,
      customModels: {},
    },
  };
}

/** `/model`: opened empty, then filled by the backend's chat options. */
export function openChatModelMenu(state: SessionState): SessionState {
  return {
    ...state,
    overlay: null,
    themePicker: null,
    chatMenu: {
      kind: 'model',
      title: 'Harness and model',
      rows: [{kind: 'note', label: 'Loading options…'}],
      selected: -1,
      pending: true,
      error: null,
      customModels: {},
    },
  };
}

/**
 * Renders exactly what the backend returned: one group per provider it says is
 * valid, its models beneath, and a free-text entry per group for a model the
 * suggestion list does not carry.
 */
export function setChatModelMenuOptions(state: SessionState, options: ChatOptions): SessionState {
  const menu = state.chatMenu;
  if (menu === null || menu.kind !== 'model') return state;
  const rows: ChatMenuRow[] = [];
  for (const group of options.providers ?? []) {
    rows.push({kind: 'header', label: agentRuntimeLabel(group.provider, null) ?? group.provider});
    for (const option of group.models ?? []) {
      rows.push({
        kind: 'model',
        label: option.default ? `${option.model}  · run default` : option.model,
        provider: group.provider,
        model: option.model,
        isDefault: option.default === true,
      });
    }
    rows.push({kind: 'custom', label: 'custom model…', provider: group.provider});
  }
  if (rows.length === 0) rows.push({kind: 'note', label: 'This run offers no chat harness.'});
  return {...state, chatMenu: {...menu, rows, selected: firstSelectable(rows), pending: false}};
}

export function failChatMenu(state: SessionState, message: string): SessionState {
  const menu = state.chatMenu;
  if (menu === null) return state;
  return {
    ...state,
    chatMenu: {
      ...menu,
      rows: [{kind: 'note', label: message}],
      selected: -1,
      pending: false,
      error: message,
    },
  };
}

export function moveChatMenuSelection(state: SessionState, delta: number): SessionState {
  const menu = state.chatMenu;
  if (menu === null || delta === 0) return state;
  const step = delta > 0 ? 1 : -1;
  let selected = menu.selected;
  for (let remaining = Math.abs(delta); remaining > 0; remaining -= 1) {
    const next = nextSelectable(menu.rows, selected, step);
    if (next === selected) break;
    selected = next;
  }
  if (selected === menu.selected) return state;
  return {...state, chatMenu: {...menu, selected}};
}

export function closeChatMenu(state: SessionState): SessionState {
  if (state.chatMenu === null) return state;
  return {...state, chatMenu: null};
}

/** The row Enter acts on, or null while nothing selectable is highlighted. */
export function selectedChatMenuRow(state: SessionState): ChatMenuRow | null {
  const menu = state.chatMenu;
  if (menu === null || menu.selected < 0) return null;
  return menu.rows[menu.selected] ?? null;
}

/** Text typed into the highlighted custom entry, empty when none is. */
export function chatMenuCustomModel(state: SessionState): string {
  const row = selectedChatMenuRow(state);
  if (row === null || row.kind !== 'custom') return '';
  return state.chatMenu?.customModels[row.provider] ?? '';
}

export function setChatMenuCustomModel(state: SessionState, model: string): SessionState {
  const menu = state.chatMenu;
  const row = selectedChatMenuRow(state);
  if (menu === null || row === null || row.kind !== 'custom') return state;
  return {
    ...state,
    chatMenu: {...menu, customModels: {...menu.customModels, [row.provider]: model}},
  };
}

function isSelectable(row: ChatMenuRow): boolean {
  return row.kind === 'model' || row.kind === 'custom' || row.kind === 'thread';
}

function firstSelectable(rows: readonly ChatMenuRow[]): number {
  const index = rows.findIndex(isSelectable);
  return index;
}

/** The next selectable index in `step` direction, or `from` when there is none. */
function nextSelectable(rows: readonly ChatMenuRow[], from: number, step: number): number {
  for (let index = from + step; index >= 0 && index < rows.length; index += step) {
    const row = rows[index];
    if (row !== undefined && isSelectable(row)) return index;
  }
  return from < 0 ? firstSelectable(rows) : from;
}
