import {describe, expect, it} from '@effect/vitest';
import {
  APPEARANCE_MODES,
  type Appearance,
  THEMES,
  type ThemeMode,
  type ThemeName,
  themesOf,
} from '@vibesys/ui/tokens';
import fc from 'fast-check';
import {
  type AppearanceAction,
  appearanceReducer,
  defaultAppearance,
  initialAppearance,
  themeShown,
  withMode,
  withTheme,
} from './appearance.js';

const modes: fc.Arbitrary<ThemeMode> = fc.constantFrom('light', 'dark');
const themeNames = Object.keys(THEMES) as ThemeName[];
const theme = fc.constantFrom(...themeNames);
const appearance: fc.Arbitrary<Appearance> = fc.record({
  mode: fc.constantFrom(...APPEARANCE_MODES),
  light: fc.constantFrom(...themesOf('light')),
  dark: fc.constantFrom(...themesOf('dark')),
});
const action: fc.Arbitrary<AppearanceAction> = fc.oneof(
  fc.record({type: fc.constant('loaded' as const), appearance: fc.option(appearance)}),
  fc.record({type: fc.constant('chosen' as const), appearance}),
  fc.record({type: fc.constant('system' as const), mode: modes}),
  fc.constant({type: 'saved' as const}),
  fc.constant({type: 'save-failed' as const}),
);

describe('the appearance', () => {
  it('starts on the system, with each mode its default theme', () => {
    const state = initialAppearance('dark');
    expect(state.appearance).toEqual(defaultAppearance);
    expect(THEMES[themeShown(state)].mode).toBe('dark');
    expect(THEMES[themeShown(initialAppearance('light'))].mode).toBe('light');
  });

  it('shows a theme of the mode the person asked for, and of the system own mode when they asked for it', () => {
    fc.assert(
      fc.property(fc.array(action, {maxLength: 30}), modes, (steps, system) => {
        const state = steps.reduce(appearanceReducer, initialAppearance(system));
        const mode = state.appearance.mode === 'system' ? state.system : state.appearance.mode;
        expect(THEMES[themeShown(state)].mode).toBe(mode);
      }),
    );
  });

  it('follows the system live, only while the mode is system', () => {
    fc.assert(
      fc.property(appearance, modes, modes, (chosen, before, after) => {
        const start = appearanceReducer(initialAppearance(before), {
          type: 'chosen',
          appearance: chosen,
        });
        const moved = appearanceReducer(start, {type: 'system', mode: after});
        if (chosen.mode === 'system') expect(THEMES[themeShown(moved)].mode).toBe(after);
        else expect(themeShown(moved)).toBe(themeShown(start));
      }),
    );
  });

  it('choosing a theme sets that mode only and every theme can be chosen', () => {
    fc.assert(
      fc.property(appearance, theme, (before, chosen) => {
        const after = withTheme(before, chosen);
        const other = THEMES[chosen].mode === 'light' ? 'dark' : 'light';
        expect(after[THEMES[chosen].mode]).toBe(chosen);
        expect(after[other]).toBe(before[other]);
        expect(after.mode).toBe(before.mode);
      }),
    );
  });

  it('choosing a mode keeps the themes', () => {
    fc.assert(
      fc.property(appearance, fc.constantFrom(...APPEARANCE_MODES), (before, mode) => {
        expect(withMode(before, mode)).toEqual({...before, mode});
      }),
    );
  });

  it('nothing saved leaves the choice as it is, and a failed save is cleared by the next', () => {
    const state = initialAppearance('light');
    expect(appearanceReducer(state, {type: 'loaded', appearance: null})).toBe(state);
    const failed = appearanceReducer(state, {type: 'save-failed'});
    expect(failed.unsaved).toBe(true);
    expect(appearanceReducer(failed, {type: 'saved'}).unsaved).toBe(false);
  });
});
