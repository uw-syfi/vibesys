/**
 * The appearance the app shows: the person's saved choice, what the operating system asks for, and
 * whether the last save worked. The themes and their modes come from the design system's token
 * table, never repeated here, so a theme added there is offered here without a change.
 */
import {
  type Appearance,
  type AppearanceMode,
  DEFAULT_THEMES,
  effectiveTheme,
  THEMES,
  type ThemeMode,
  type ThemeName,
} from '@vibesys/ui/tokens';

export interface AppearanceState {
  readonly appearance: Appearance;
  /** What the operating system asks for now. */
  readonly system: ThemeMode;
  /** The last save failed, so the choice is on screen but would not survive a restart. */
  readonly unsaved: boolean;
}

export type AppearanceAction =
  /** What was saved earlier: `null` when nothing was. */
  | {readonly type: 'loaded'; readonly appearance: Appearance | null}
  | {readonly type: 'chosen'; readonly appearance: Appearance}
  | {readonly type: 'system'; readonly mode: ThemeMode}
  | {readonly type: 'saved'}
  | {readonly type: 'save-failed'};

/** Follow the system, with each mode's default theme. */
export const defaultAppearance: Appearance = {
  mode: 'system',
  light: DEFAULT_THEMES.light,
  dark: DEFAULT_THEMES.dark,
};

export const initialAppearance = (system: ThemeMode): AppearanceState => ({
  appearance: defaultAppearance,
  system,
  unsaved: false,
});

export function appearanceReducer(
  state: AppearanceState,
  action: AppearanceAction,
): AppearanceState {
  switch (action.type) {
    case 'loaded':
      return action.appearance === null ? state : {...state, appearance: action.appearance};
    case 'chosen':
      return {...state, appearance: action.appearance};
    case 'system':
      return {...state, system: action.mode};
    case 'saved':
      return {...state, unsaved: false};
    case 'save-failed':
      return {...state, unsaved: true};
  }
}

/** The theme the page shows now. */
export const themeShown = (state: AppearanceState): ThemeName =>
  effectiveTheme(state.appearance, state.system);

/** `appearance` with the mode `mode`; the themes are kept. */
export const withMode = (appearance: Appearance, mode: AppearanceMode): Appearance => ({
  ...appearance,
  mode,
});

/** `appearance` with `theme` as the theme of its own mode; the other mode's theme is kept. */
export const withTheme = (appearance: Appearance, theme: ThemeName): Appearance =>
  THEMES[theme].mode === 'light' ? {...appearance, light: theme} : {...appearance, dark: theme};
