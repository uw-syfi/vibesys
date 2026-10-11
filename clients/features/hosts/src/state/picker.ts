/**
 * The "Connect to host" sheet's store: a searchable list of `~/.ssh/config` aliases, or the
 * destination typed, with a keyboard selection that always names a listed row (or none when the
 * list is empty). Choosing a row asks the shell to show that host's panel (`show-host`).
 */

import {type HostKey, validAlias} from '@vibesys/hosts';
import type {WelcomeResult} from '@vibesys/ipc';
import {only, type Step} from '@vibesys/store';

/** One row of the host list: an alias from `~/.ssh/config`, or the destination the user typed. */
export interface HostChoice {
  readonly alias: string;
  readonly typed: boolean;
}

function usable(alias: string): boolean {
  try {
    validAlias(alias);
    return true;
  } catch {
    return false;
  }
}

/**
 * The hosts matching `query` (case-insensitive substring of the alias), in config order, then the
 * query itself as a typed destination (`user@host` or an alias ssh resolves) when it is usable
 * and not already listed.
 */
export function filterHosts(aliases: readonly string[], query: string): HostChoice[] {
  const needle = query.trim().toLowerCase();
  const matches = aliases
    .filter(alias => alias.toLowerCase().includes(needle))
    .map(alias => ({alias, typed: false}));
  const typed = query.trim();
  if (typed !== '' && usable(typed) && !aliases.includes(typed)) {
    matches.push({alias: typed, typed: true});
  }
  return matches;
}

/** The selection after moving `delta` rows from `index` in a list of `length`, wrapping around. */
export function moveSelection(index: number, delta: number, length: number): number {
  if (length <= 0) return -1;
  const start = index < 0 ? (delta > 0 ? -1 : 0) : index;
  return (((start + delta) % length) + length) % length;
}

type Aliases =
  | {readonly kind: 'loading'; readonly token: number}
  | {readonly kind: 'loaded'; readonly list: readonly string[]}
  | {readonly kind: 'failed'; readonly error: string};

export type PickerState =
  | {readonly kind: 'closed'; readonly next: number}
  | {
      readonly kind: 'open';
      readonly next: number;
      readonly aliases: Aliases;
      readonly query: string;
      /** The selected row of `pickerChoices`, -1 when there is none. */
      readonly selected: number;
    };

export type PickerAction =
  | {readonly type: 'open'}
  | {readonly type: 'close'}
  | {readonly type: 'query'; readonly value: string}
  | {readonly type: 'move'; readonly delta: number}
  /** Enter (the selected row), or a click on row `index`. */
  | {readonly type: 'choose'; readonly index?: number}
  | {
      readonly type: 'aliases-reply';
      readonly token: number;
      readonly result: WelcomeResult<readonly string[]>;
    };

export type PickerRequest =
  | {readonly kind: 'aliases'; readonly token: number}
  | {readonly kind: 'show-host'; readonly host: HostKey};

export const PICKER_CLOSED: PickerState = {kind: 'closed', next: 1};

export function pickerChoices(state: PickerState): HostChoice[] {
  if (state.kind !== 'open') return [];
  const list = state.aliases.kind === 'loaded' ? state.aliases.list : [];
  return filterHosts(list, state.query);
}

export function pickerInvariants(state: PickerState): string[] {
  if (state.kind !== 'open') return [];
  const count = pickerChoices(state).length;
  if (count === 0 ? state.selected !== -1 : state.selected < 0 || state.selected >= count) {
    return [`selection ${state.selected} of ${count} rows`];
  }
  return [];
}

/** `state` with its selection clamped onto the rows it lists. */
function clamped(state: Extract<PickerState, {kind: 'open'}>): PickerState {
  const count = pickerChoices(state).length;
  const selected = count === 0 ? -1 : Math.min(Math.max(state.selected, 0), count - 1);
  return {...state, selected};
}

export function pickerStep(
  state: PickerState,
  action: PickerAction,
): Step<PickerState, PickerRequest> {
  if (action.type === 'open') {
    if (state.kind === 'open') return only(state);
    const token = state.next;
    return {
      state: clamped({
        kind: 'open',
        next: token + 1,
        aliases: {kind: 'loading', token},
        query: '',
        selected: 0,
      }),
      requests: [{kind: 'aliases', token}],
    };
  }
  if (state.kind === 'closed') return only(state);
  switch (action.type) {
    case 'close':
      return only({kind: 'closed', next: state.next});
    case 'query':
      return only(clamped({...state, query: action.value, selected: 0}));
    case 'move':
      return only({
        ...state,
        selected: moveSelection(state.selected, action.delta, pickerChoices(state).length),
      });
    case 'choose': {
      const choice = pickerChoices(state)[action.index ?? state.selected];
      if (choice === undefined) return only(state);
      return {
        state: {kind: 'closed', next: state.next},
        requests: [{kind: 'show-host', host: `ssh:${choice.alias}`}],
      };
    }
    case 'aliases-reply': {
      if (state.aliases.kind !== 'loading' || state.aliases.token !== action.token) {
        return only(state);
      }
      const aliases: Aliases = action.result.ok
        ? {kind: 'loaded', list: action.result.value}
        : {kind: 'failed', error: action.result.error};
      return only(clamped({...state, aliases}));
    }
  }
}
