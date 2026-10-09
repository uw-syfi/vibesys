import type {SessionState} from './session-model.js';
import {THEME_NAMES, type ThemeName} from './theme.js';

/** Pure state transitions for the terminal theme picker. */
export function setTheme(state: SessionState, themeName: ThemeName): SessionState {
  if (state.themeName === themeName) {
    return state.themePicker === null ? state : {...state, themePicker: null};
  }
  return {...state, themeName, overlay: null, themePicker: null};
}

export function openThemePicker(state: SessionState): SessionState {
  return {
    ...state,
    overlay: null,
    palette: null,
    notepad: {...state.notepad, open: false},
    themePicker: {selected: state.themeName},
  };
}

export function moveThemeSelection(state: SessionState, delta: number): SessionState {
  const picker = state.themePicker;
  if (picker === null) return state;
  const current = THEME_NAMES.indexOf(picker.selected);
  const index = Math.min(THEME_NAMES.length - 1, Math.max(0, current + delta));
  const selected = THEME_NAMES[index];
  return selected === undefined || selected === picker.selected
    ? state
    : {...state, themePicker: {selected}};
}

export function closeThemePicker(state: SessionState): SessionState {
  return state.themePicker === null ? state : {...state, themePicker: null};
}
