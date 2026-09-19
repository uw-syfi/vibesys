import {chatPaneFocused, type SessionState} from './session-model.js';

/** The palette's typed filter and highlighted row; the match list is derived, not stored. */
export interface CommandPaletteState {
  readonly query: string;
  readonly selected: number;
}

/** Which composer holds the keys, and so which one the palette opened over. */
export function activeCommandSurface(state: SessionState): 'command' | 'chat' {
  return state.chatOpen || chatPaneFocused(state) ? 'chat' : 'command';
}

/** Opens the command palette, starting on an empty query with nothing typed yet. */
export function openPalette(state: SessionState): SessionState {
  return {...state, overlay: null, themePicker: null, palette: {query: '', selected: 0}};
}

export function closePalette(state: SessionState): SessionState {
  if (state.palette === null) return state;
  return {...state, palette: null};
}

/** Replaces the typed filter. A changed query starts back on the first match. */
export function setPaletteQuery(state: SessionState, query: string): SessionState {
  const palette = state.palette;
  if (palette === null || palette.query === query) return state;
  return {...state, palette: {query, selected: 0}};
}

/**
 * Wraps like `SuggestionMenu.navigate`, not clamps like the theme picker: the
 * list is refiltered on every keystroke, so a selection stuck at an edge
 * would strand as that list moves under it. `matchCount` comes from the
 * caller because the match list itself is derived, not stored.
 */
export function movePaletteSelection(
  state: SessionState,
  delta: number,
  matchCount: number,
): SessionState {
  const palette = state.palette;
  if (palette === null || matchCount === 0) return state;
  const selected = (palette.selected + delta + matchCount) % matchCount;
  if (selected === palette.selected) return state;
  return {...state, palette: {...palette, selected}};
}
