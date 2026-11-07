import {describe, expect, it} from '@effect/vitest';
import {DesktopAppearanceMode, DesktopTheme} from '@vibesys/proto';
import {APPEARANCE_MODES, type Appearance, THEMES, themesOf} from '@vibesys/ui/tokens';
import fc from 'fast-check';
import {appearanceFromBytes, appearanceToBytes} from './preferences-codec.js';

const appearance: fc.Arbitrary<Appearance> = fc.record({
  mode: fc.constantFrom(...APPEARANCE_MODES),
  light: fc.constantFrom(...themesOf('light')),
  dark: fc.constantFrom(...themesOf('dark')),
});

const enumNames = (values: object): string[] =>
  Object.values(values)
    .filter((name): name is string => typeof name === 'string' && !name.startsWith('UNSPECIFIED'))
    .map(name => name.toLowerCase());

describe('the preferences message', () => {
  it('carries any appearance unchanged', () => {
    fc.assert(
      fc.property(appearance, chosen => {
        expect(appearanceFromBytes(appearanceToBytes(chosen))).toEqual(chosen);
      }),
    );
  });

  it('names exactly the themes the design system defines, so adding a theme is a token block and an enum value', () => {
    expect(enumNames(DesktopTheme).sort()).toEqual(Object.keys(THEMES).sort());
  });

  it('names exactly the modes the app offers', () => {
    expect(enumNames(DesktopAppearanceMode).sort()).toEqual([...APPEARANCE_MODES].sort());
  });

  it('refuses bytes that are not a valid message, naming what is wrong', () => {
    expect(() => appearanceFromBytes(Uint8Array.of(0xff, 0xff))).toThrow();
    expect(() => appearanceFromBytes(new Uint8Array())).toThrow(/mode/);
  });
});
